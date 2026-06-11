"""
qa.py — 课程问答总入口(集成 planner + retrieval + program_lookup + answer)
自然语言问题 -> planner 出查询计划 -> 按 mode 路由 -> answer 生成 grounded 中文回答。

mode 路由:
  filter   -> retrieval.filter_search(结构化过滤)
  semantic -> retrieval.semantic_search(向量+关键词 RRF)
  hybrid   -> retrieval.hybrid_search(结构化 + 语义)
  program  -> program_lookup(course_to_programs / program_to_courses)
  empty    -> 问题太宽泛/无法形成检索条件(planner 抛 ValueError 时的优雅兜底)

用法:
    python qa.py "CS有哪些课程没有考试"
    python qa.py "CSSE1001是哪些专业的必修"
    python qa.py --no-gen "..."        # 只检索不生成回答
"""
from __future__ import annotations
import os
import argparse

import psycopg

from app.services import planner, retrieval, program_lookup, answer

from app.core.config import DSN
ANSWER_CAP = 20      # 喂给答案模型的最多课程数(过多无意义且拉长 prompt)
PROGRAM_CAP = 15     # course_to_programs 喂给答案模型的最多 program 数
EMPTY_MSG = "问题太宽泛或无法形成检索条件,请补充学科方向或筛选条件(如学期 / 有无考试 / 专业)。"
REQ_LABEL = {"core": "必修", "elective": "选修"}
_CN_NUM = {2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九", 10: "十"}


def _choice_word(k: int) -> str:
    """k 个等价选项 -> 「N选一」(2->二选一、3->三选一…超表用阿拉伯数字)。"""
    return f"{_CN_NUM.get(k, k)}选一"


def _requirement(question: str) -> str | None:
    """从问题确定性判断要 core 还是 elective(选修优先于必修关键词,三态)。"""
    if any(w in question for w in ("选修", "elective")):
        return "elective"
    if any(w in question for w in ("核心", "必修", "compulsory", "core")):
        return "core"
    return None


def _c2p_rank(r: dict) -> int:
    """c2p 去重优先级:真必修 0 > 二选一核心 1 > 选修 2(越小越优先)。"""
    if r.get("requirement_type") == "core":
        return 0 if not r.get("equiv_group") else 1
    return 2


def _ans_c2p(code: str, program_facts: list) -> str:
    """course_to_programs 的确定性回答(枚举,不走 LLM,零虚构)。

    区分三态:真·必修(core 且非二选一)/ 二选一核心(core 且属 equivalence 组,可换等价课)
    / 选修。program_facts 已按 program_id 去重(每专业一行)。
    """
    if not program_facts:
        return f"{code} 不在任何已收录专业的课表中。"
    hard, oneof, elec = set(), set(), set()
    oneof_sizes: set[int] = set()
    for p in program_facts:
        t = p["title"]
        if p.get("requirement_type") == "core":
            if p.get("equiv_group"):
                oneof.add(t)
                oneof_sizes.add(len(p["equiv_group"].split("|")))
            else:
                hard.add(t)
        else:
            elec.add(t)
    oneof -= hard                    # 同专业既真必修又多选一时,以真必修为准
    elec -= (hard | oneof)
    word = (_choice_word(next(iter(oneof_sizes))) if len(oneof_sizes) == 1 else "多选一") + "核心"
    n = len(hard) + len(oneof) + len(elec)
    head = (f"{code} 出现在 {n} 个专业的课表中(必修 {len(hard)} 个"
            + (f"、{word} {len(oneof)} 个" if oneof else "")
            + f"、选修 {len(elec)} 个)。")
    out = [head]
    if hard:
        out.append("必修于:" + "、".join(sorted(hard)[:8]) + (f" 等 {len(hard)} 个。" if len(hard) > 8 else "。"))
    if oneof:
        out.append(f" {word}于:" + "、".join(sorted(oneof)[:6]) + (f" 等 {len(oneof)} 个。" if len(oneof) > 6 else "。"))
    if elec:
        out.append(" 选修于:" + "、".join(sorted(elec)[:6]) + (f" 等 {len(elec)} 个。" if len(elec) > 6 else "。"))
    return "".join(out)


def _collapse_slots(courses: list) -> list[dict]:
    """把 equivalence(二选一)组按 (course_list, equiv_group) 折成 1 个槽位。
    返回 [{codes:[...], titles:[...], is_group:bool}];standalone 课各自成槽,顺序稳定。"""
    slots: list[dict] = []
    groups: dict = {}
    for c in courses:
        g = c.get("equiv_group") or ""
        if not g:
            slots.append({"codes": [c["code"]], "titles": [c.get("title")], "is_group": False})
            continue
        key = (c.get("course_list"), g)
        slot = groups.get(key)
        if slot is None:
            slot = {"codes": [], "titles": [], "is_group": True}
            groups[key] = slot
            slots.append(slot)
        slot["codes"].append(c["code"])
        slot["titles"].append(c.get("title"))
    return slots


def _ans_p2c(title: str, req: str | None, courses: list) -> str:
    """program_to_courses 的确定性回答(枚举,不走 LLM)。等价组折成 1 门、按组大小措辞、
    并始终单独列出(避免被单课截断挡掉)。"""
    if not courses:
        return f"未找到 {title} 的相关课程。"
    slots = _collapse_slots(courses)
    n = len(slots)
    groups = [s for s in slots if s["is_group"]]
    singles = [s for s in slots if not s["is_group"]]
    sizes = {len(s["codes"]) for s in groups}
    word = _choice_word(next(iter(sizes))) if len(sizes) == 1 else "多选一"

    SHOW = 12
    parts = [s["codes"][0] + (f"({s['titles'][0]})" if s["titles"][0] else "") for s in singles[:SHOW]]
    if len(singles) > SHOW:
        parts.append("等")
    seg = []
    if parts:
        seg.append("、".join(parts))
    if groups:
        seg.append(f"可{word}项:" + "、".join(" 或 ".join(s["codes"]) for s in groups))
    grp_note = f"(其中 {len(groups)} 门{word})" if groups else ""
    return f"{title} 的{REQ_LABEL.get(req, '')}课共 {n} 门{grp_note}:{';'.join(seg)}。"


def _ans_permit(code: str, title: str, excluded: bool, owns: list) -> str:
    """"某专业能否修某课"的确定性回答(基于 program_exclude 禁课表,零虚构)。"""
    if excluded:
        return f"不能。{title} 明确规定不计学分(No credit will be given for {code})—— 修了也拿不到学分。"
    if owns:
        kind = "核心/必修" if any(r["requirement_type"] == "core" for r in owns) else "选修"
        return f"可以。{title} 未禁修 {code},且它就在该专业课表里(作为{kind}课)。"
    return (f"{title} 未把 {code} 列为禁修课;但它也不在该专业的指定课表中,"
            f"能否作为通选(general elective)计入要看学分/层级分布规则——本库暂未覆盖该判定。")


def _retrieve(conn, question: str) -> dict:
    """检索 + 路由,返回结构化结果(不含 LLM 生成的 answer);program 模式确定性回答放 prog_answer。
    mode='empty' 时其余字段为空。run 与 run_stream 共用,避免重复整段路由逻辑。"""
    try:
        schema = planner.build_schema_doc(conn)
        p = planner.plan(question, schema, conn)
    except ValueError as e:                     # 宽泛/无法规划 -> 优雅兜底,不抛 traceback
        return {"plan": None, "mode": "empty", "meta": str(e), "courses": [],
                "program_facts": None, "prog_answer": None}

    mode = p["mode"]
    courses: list[dict] = []
    program_facts = None
    meta = ""
    prog_answer = None      # program 模式的确定性回答(不走 LLM)

    if mode == "filter":
        courses = retrieval.filter_search(conn, p["where"])
        meta = f"WHERE {p['where']}"
    elif mode == "semantic":
        courses = retrieval.semantic_search(conn, p["semantic_query"])
        meta = f"semantic='{p['semantic_query']}'"
    elif mode == "hybrid":
        courses = retrieval.hybrid_search(conn, p["where"] or None, p["semantic_query"])
        meta = f"WHERE {p['where']} + semantic='{p['semantic_query']}'"
    elif mode == "program":
        if p.get("direction") == "permit":
            code = p["course_code"]
            progs = program_lookup.find_program(conn, p.get("program_name") or "")
            if progs:
                pid, title = progs[0]
                excluded = program_lookup.is_excluded(conn, pid, code)
                owns = [r for r in program_lookup.programs_for_course(conn, code)
                        if r["program_id"] == pid]
                program_facts = {"program": title, "course": code,
                                 "excluded": excluded, "in_program": bool(owns)}
                meta = f"{code} @ program='{title}' 能否修(禁课={excluded})"
                prog_answer = _ans_permit(code, title, excluded, owns)
            else:
                name = p.get("program_name") or ""
                meta = f"未找到 program '{name}'"
                prog_answer = f"未找到名为「{name}」的专业,试试全称(如 Bachelor of Computer Science)。"
        elif p.get("direction") == "program_to_courses":
            progs = program_lookup.find_program(conn, p.get("program_name") or "")
            if progs:
                pid, title = progs[0]
                req = _requirement(question)
                rows = program_lookup.courses_for_program(conn, pid, requirement_type=req)
                courses = [{**c, "code": c.get("course_code")} for c in rows]  # 归一化 code 键
                program_facts = {"program": title, "requirement": req or "all"}
                pick = f"(从 {len(progs)} 个匹配中选第一个)" if len(progs) > 1 else ""
                meta = f"program='{title}'{pick} 的{REQ_LABEL.get(req, '')}课程"
                prog_answer = _ans_p2c(title, req, courses)
                # B: 该专业还有 plan 层(major/方向)核心课时补提示(direct 查询不展示这些)
                if req != "elective" and program_lookup.has_plan_level_core(conn, pid):
                    prog_answer += " 注:该专业含 major/方向,其核心课需选定方向后确定,可用选课模拟器查看。"
                # 禁课标注:该专业明确不计学分的课
                ex = program_lookup.excluded_courses(conn, pid)
                if ex:
                    tail = f" 等 {len(ex)} 门" if len(ex) > 8 else ""
                    prog_answer += f" 该专业禁修(不计学分):{'、'.join(ex[:8])}{tail}。"
            else:
                name = p.get("program_name") or ""
                meta = f"未找到 program '{name}'"
                prog_answer = f"未找到名为「{name}」的专业,试试全称(如 Bachelor of Computer Science)。"
        else:  # course_to_programs
            code = p["course_code"]
            by_prog: dict = {}                          # 每专业一行;真必修 > 二选一核心 > 选修
            for r in program_lookup.programs_for_course(conn, code):
                pid = r["program_id"]
                if pid not in by_prog or _c2p_rank(r) < _c2p_rank(by_prog[pid]):
                    by_prog[pid] = r
            program_facts = sorted(by_prog.values(),
                                   key=lambda r: (_c2p_rank(r), r["title"]))
            meta = f"{code} 所属 program(共 {len(program_facts)})"
            prog_answer = _ans_c2p(code, program_facts)
            # 禁课标注:明确禁修该课的专业
            excl_progs = program_lookup.programs_excluding(conn, code)
            if excl_progs:
                eg = excl_progs[0][1]
                prog_answer += f" 另有 {len(excl_progs)} 个专业明确禁修该课(不计学分),如 {eg}。"

    return {"plan": p, "mode": mode, "meta": meta,
            "courses": courses, "program_facts": program_facts, "prog_answer": prog_answer}


def run(conn, question: str, generate: bool = True) -> dict:
    r = _retrieve(conn, question)
    if r["mode"] == "empty":
        return {"plan": None, "mode": "empty", "meta": r["meta"], "courses": [],
                "program_facts": None, "answer": EMPTY_MSG if generate else None}
    ans = None
    if generate:
        ans = r["prog_answer"] if r["mode"] == "program" else \
            answer.answer(question, r["courses"][:ANSWER_CAP],
                          _gen_facts(r["courses"], r["program_facts"]))
    return {"plan": r["plan"], "mode": r["mode"], "meta": r["meta"],
            "courses": r["courses"], "program_facts": r["program_facts"], "answer": ans}


def run_stream(conn, question: str):
    """流式问答,依次 yield (event, data):
       ('meta', {mode, meta, courses, program_facts}) -> ('token', delta)... -> ('done', 完整答案)。
       empty 给固定兜底句;program 答案确定性(单块);其余模式逐 token 流式 + 收尾护栏。"""
    r = _retrieve(conn, question)
    mode = r["mode"]
    yield ("meta", {"mode": mode, "meta": r["meta"],
                    "courses": r["courses"], "program_facts": r["program_facts"]})

    if mode == "empty":
        yield ("token", EMPTY_MSG)
        yield ("done", EMPTY_MSG)
        return
    if mode == "program":
        ans = r["prog_answer"] or ""
        yield ("token", ans)
        yield ("done", ans)
        return

    capped = r["courses"][:ANSWER_CAP]
    acc: list[str] = []
    for delta in answer.answer_stream(question, capped, _gen_facts(r["courses"], r["program_facts"])):
        acc.append(delta)
        yield ("token", delta)
    yield ("done", answer.guard_citations("".join(acc), capped))


def _gen_facts(courses: list[dict], program_facts):
    """组装喂给答案模型的补充事实:截断 program 列表、超量课程补总数(answer 会据此报总数)。"""
    if isinstance(program_facts, list):                 # course_to_programs:截断 program 列表
        return {"所属program总数": len(program_facts), "programs": program_facts[:PROGRAM_CAP]} \
            if len(program_facts) > PROGRAM_CAP else program_facts
    pf = dict(program_facts) if isinstance(program_facts, dict) else {}
    if len(courses) > ANSWER_CAP:
        pf["命中总数"] = len(courses)
    return pf or program_facts


def _print(res: dict):
    print(f"[mode={res['mode']}] {res['meta']}")
    plan = res["plan"] or {}
    if res["mode"] == "program" and plan.get("direction") == "course_to_programs":
        rows = res["program_facts"] or []
        print(f"命中 {len(rows)} 个 program" + ("(前15)" if len(rows) > 15 else "") + ":")
        for r in rows[:15]:
            via = f" (via {r['via_plan']})" if r.get("via_plan") else ""
            print(f"  [{r['requirement_type']}] {r['title']} — {r['course_list']}{via}")
    elif res["mode"] != "empty":
        rows = res["courses"]
        print(f"命中 {len(rows)} 门" + ("(前10)" if len(rows) > 10 else "") + ":")
        for c in rows[:10]:
            sim = f"  sim={c['sim']:.3f}" if c.get("sim") is not None else ""
            print(f"  {c.get('code')}  {c.get('title') or ''}  "
                  f"({c.get('semester') or '?'},{c.get('level') or '?'},exam={c.get('has_exam')}){sim}")
    if res["answer"] is not None:
        print(f"\n【回答】\n{res['answer']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--no-gen", action="store_true", help="只检索不生成回答")
    args = ap.parse_args()
    with psycopg.connect(DSN) as conn:
        retrieval.ensure_fts_index(conn)        # 一次性确保 FTS 索引(读路径不再建索引)
        res = run(conn, args.question, generate=not args.no_gen)
    _print(res)


if __name__ == "__main__":
    main()
