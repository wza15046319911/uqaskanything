"""
qa.py — 课程问答总入口(集成 planner + retrieval + program_lookup + answer)
自然语言问题 -> planner 出查询计划 -> 按 mode 路由 -> answer 生成 grounded 中文回答。

mode 路由:
  filter   -> retrieval.filter_search(结构化过滤)
  semantic -> retrieval.semantic_search(向量+关键词 RRF)
  hybrid   -> retrieval.hybrid_search(结构化 + 语义)
  program  -> program_lookup(course_to_programs / program_to_courses)
  kb       -> retrieval.kb_search(知识库 FAQ/日期/政策,planner 前置分类判定)
  empty    -> 问题太宽泛/无法形成检索条件(planner 抛 ValueError 时的优雅兜底)

用法:
    python qa.py "CS有哪些课程没有考试"
    python qa.py "CSSE1001是哪些专业的必修"
    python qa.py --no-gen "..."        # 只检索不生成回答
"""
from __future__ import annotations
import os
import re
import argparse

import psycopg

from app.services import planner, retrieval, program_lookup, answer, answerability

from app.core.config import DSN
ANSWER_CAP = 20      # 喂给答案模型的最多课程数(过多无意义且拉长 prompt)
PROGRAM_CAP = 15     # course_to_programs 喂给答案模型的最多 program 数
KB_PREFER_SIM = 0.55  # 课程语义 top sim 低于此且知识库召回更强时,转知识库(FAQ/article)
KB_STRONG_SIM = 0.62  # filter 命中空时,知识库 top sim 达此高门槛才转(防弱相关误转,如校区课查询)
# _kb_or_none 软门槛:0.60-0.62 的贴阈命中不直接拒,交 answerability 双门(实体/年份 + P2 LLM)裁定。
# 救「正确页恰好贴在 0.62 阈值下」的真问题(中文查英文 KB 的词序抖动,如「怎么开在读证明」0.618);
# 虚构仍被双门挡(实体缺席 / LLM 门)。判别精度归 answerability,不是单纯放低阈值放水。
KB_SOFT_SIM = 0.60
# 日期/时点意图词:问的是「什么时候/哪天」而非课程本身 -> 即便 filter 命中课也该转知识库(学术日历)
_DATE_INTENT = re.compile(r"什么时候|何时|哪天|几号|日期|开学|开课|放假|census|截止|deadline|when|start\s*date", re.I)
# 真正的课程筛选维度:where 含这些才算「在查课程」;只有 year/semester 则是时间限定,非课程筛选
_COURSE_DIM = re.compile(r"\b(level|units|has_exam|has_hurdle|location|attendance_mode)\b", re.I)
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


def _ans_c2p(code: str, program_facts: list, req: str | None = None) -> str:
    """course_to_programs 的确定性回答(枚举,不走 LLM,零虚构)。

    区分三态:真·必修(core 且非二选一)/ 二选一核心(core 且属 equivalence 组,可换等价课)
    / 选修。program_facts 已按 program_id 去重(每专业一行)。
    req 过滤:'core' 只列必修+二选一核心、'elective' 只列选修、None 三态全列。
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
    if req == "core":                # 用户限定必修/核心:不列选修(二选一核心仍属 core)
        elec = set()
    elif req == "elective":          # 用户限定选修:不列必修与二选一核心
        hard, oneof = set(), set()
    word = (_choice_word(next(iter(oneof_sizes))) if len(oneof_sizes) == 1 else "多选一") + "核心"
    n = len(hard) + len(oneof) + len(elec)
    scope = {"core": "必修", "elective": "选修"}.get(req, "")
    bd = ([f"必修 {len(hard)} 个"] if req != "elective" else []) \
        + ([f"{word} {len(oneof)} 个"] if oneof else []) \
        + ([f"选修 {len(elec)} 个"] if req != "core" else [])
    head = f"{code} 出现在 {n} 个专业的{scope}课表中(" + "、".join(bd) + ")。"
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


def _ans_program_filter(title: str, courses: list) -> str:
    """组合查询(专业范围 + 结构化筛选)的确定性回答:扁平列出命中课,不走 LLM,零虚构。"""
    if not courses:
        return f"{title} 的课表中没有符合条件的课程。"
    SHOW = 20
    parts = [f"{c['code']}({c['title']})" if c.get("title") else c["code"]
             for c in courses[:SHOW]]
    tail = f" 等 {len(courses)} 门" if len(courses) > SHOW else ""
    return f"{title} 课表中符合条件的共 {len(courses)} 门:" + "、".join(parts) + tail + "。"


def _first_digit(code: str | None) -> str:
    """course code 的首位数字(=课程级别号);无数字返回空串。"""
    for ch in code or "":
        if ch.isdigit():
            return ch
    return ""


def _ans_low_burden(courses: list) -> str:
    """「低负担/躺平」查询的确定性回答:已按客观考核负担(无考试/无 hurdle/考核项最少)排序。
    诚实声明系统不判断难度/通过率(红线1:难度无数据,绝不编),不走 LLM。"""
    if not courses:
        return ("没有同时满足「无考试且无 hurdle」的课。可以放宽条件,比如只要「没有考试」"
                "或「没有 hurdle」再试。")
    SHOW = 20
    parts = [f"{c['code']}({c['title']})" if c.get("title") else c["code"]
             for c in courses[:SHOW]]
    tail = f" 等共 {len(courses)} 门" if len(courses) > SHOW else f",共 {len(courses)} 门"
    return ("按客观考核负担排序(均无考试、无 hurdle,考核项由少到多):"
            + "、".join(parts) + tail
            + "。注:系统不判断课程难度或通过率,以上仅按考核结构排序,"
              "请结合课程大纲(ECP)与你的基础自行评估。")


def _ans_permit(code: str, title: str, excluded: bool, owns: list) -> str:
    """"某专业能否修某课"的确定性回答(基于 program_exclude 禁课表,零虚构)。"""
    if excluded:
        return f"不能。{title} 明确规定不计学分(No credit will be given for {code})—— 修了也拿不到学分。"
    if owns:
        kind = "核心/必修" if any(r["requirement_type"] == "core" for r in owns) else "选修"
        return f"可以。{title} 未禁修 {code},且它就在该专业课表里(作为{kind}课)。"
    return (f"{title} 未把 {code} 列为禁修课;但它也不在该专业的指定课表中,"
            f"能否作为通选(general elective)计入要看学分/层级分布规则——本库暂未覆盖该判定。")


_ATTEND_VAL_RE = re.compile(r"attendance_mode\s*=\s*'([^']*)'", re.I)
_LOC_VAL_RE = re.compile(r"location\s*=\s*'([^']*)'", re.I)


def _empty_note(where: str) -> str:
    """筛选命中空、且 where 指定了「库里根本没有」的授课模式/校区值时,给确定性明确提示
    (而非笼统「太宽泛」):让学生知道是数据本身无此值,不是问法不对。枚举取 planner 实时缓存。"""
    if not where:
        return ""
    am = _ATTEND_VAL_RE.search(where)
    if am and am.group(1).strip().lower() not in planner._ENUM_CACHE.get("attendance_mode", set()):
        return (f"本库收录的课程授课模式均为面授(In Person),暂无「{am.group(1)}」授课模式的课程数据;"
                f"请到 UQ 官方课程页核对具体课程的授课方式。")
    loc = _LOC_VAL_RE.search(where)
    if loc and loc.group(1).strip().lower() not in planner._ENUM_CACHE.get("location", set()):
        return f"本库暂无「{loc.group(1)}」校区的课程数据,请确认校区名称或换用已收录校区再试。"
    return ""


def _retrieve(conn, question: str) -> dict:
    """检索 + 路由,返回结构化结果(不含 LLM 生成的 answer);program 模式确定性回答放 prog_answer。
    mode='empty' 时其余字段为空。run 与 run_stream 共用,避免重复整段路由逻辑。"""
    try:
        schema = planner.build_schema_doc(conn)
        p = planner.plan(question, schema, conn)
    except ValueError as e:                     # 无法规划课程查询 -> 先试知识库,再优雅兜底
        chunks = _kb_or_none(conn, question)
        if chunks:
            return {"plan": None, "mode": "kb",
                    "meta": "kb(无法规划课程查询,转知识库)", "courses": [],
                    "program_facts": None, "prog_answer": None, "chunks": chunks}
        return {"plan": None, "mode": "empty", "meta": str(e), "courses": [],
                "program_facts": None, "prog_answer": None, "chunks": []}

    mode = p["mode"]
    courses: list[dict] = []
    program_facts = None
    meta = ""
    prog_answer = None      # program 模式的确定性回答(不走 LLM)
    det_answer = None       # 其它确定性答案槽(如低负担/躺平),非空则 run 直接用,绕开 LLM

    # kb 前置路由:planner 已判定为学校事务/政策/日期问题 -> 直接走知识库,不碰课程库。
    # chunks 为空时交由上层 answer_kb 出拒答(带官方链接),不退成宽泛 empty。
    if mode == "kb":
        return {"plan": p, "mode": "kb", "meta": "kb(分类→知识库)", "courses": [],
                "program_facts": None, "prog_answer": None,
                "chunks": _kb_or_none(conn, question, p.get("kb_query"))}

    # 单课详情:planner 已判定为「介绍/先修/考核某门课」-> 取该课完整信息。
    if mode == "course_detail":
        course = retrieval.course_detail(conn, p["course_code"])
        if not course:
            return {"plan": p, "mode": "empty", "meta": f"未找到课程 {p['course_code']}",
                    "courses": [], "program_facts": None, "prog_answer": None, "chunks": []}
        return {"plan": p, "mode": "course_detail", "meta": f"course_detail {p['course_code']}",
                "courses": [], "program_facts": None, "prog_answer": None,
                "chunks": [], "course": course}

    cu = p.get("coord_units") or None           # 学科→学院限定(确定性,参数化 SQL),None=不限
    cu_note = f" @units{cu}" if cu else ""
    if mode == "filter":
        try:                                    # guard_where 是硬安全网,失败时降级 empty 而非 500 给学生
            ex_title = p.get("exclude_title") or None
            if p.get("both_semesters"):
                # 「S1 和 S2 都满足」:跨学期合取,同一课两学期各有满足条件的 offering 才算
                courses = retrieval.filter_search_both_semesters(
                    conn, p["where"] or None, coord_units=cu, exclude_title=ex_title)
            else:
                courses = retrieval.filter_search(conn, p["where"],
                                                  order_by=p.get("order") or "code", coord_units=cu,
                                                  exclude_title=ex_title)
        except ValueError as e:
            return {"plan": p, "mode": "empty", "meta": f"非法 where 被安全网拦截:{e}",
                    "courses": [], "program_facts": None, "prog_answer": None, "chunks": []}
        ex_note = f" NOT_TITLE{p['exclude_title']}" if p.get("exclude_title") else ""
        meta = (("WHERE S1∩S2 都满足 " if p.get("both_semesters") else "WHERE ") + (p["where"] or "(仅两学期都开)")
                + (f" ORDER {p['order']}" if p.get("order") else "") + cu_note + ex_note)
        # 「低负担/躺平」:难度无数据,确定性出客观负担答案(红线1,不交 LLM 编难度)
        if p.get("order") == "assessments_asc":
            det_answer = _ans_low_burden(courses)
        # 命中空且问的是库里没有的授课模式/校区值 -> 确定性明确提示(而非笼统兜底/误转 KB)
        if not courses and not det_answer:
            det_answer = _empty_note(p["where"])
    elif mode == "semantic":
        courses = retrieval.semantic_search(conn, p["semantic_query"], coord_units=cu)
        meta = f"semantic='{p['semantic_query']}'" + cu_note
    elif mode == "hybrid":
        try:                                    # where 过不了安全网时退成纯语义检索,保住主题召回
            courses = retrieval.hybrid_search(conn, p["where"] or None, p["semantic_query"],
                                              coord_units=cu)
            meta = f"WHERE {p['where']} + semantic='{p['semantic_query']}'" + cu_note
        except ValueError:
            courses = retrieval.semantic_search(conn, p["semantic_query"], coord_units=cu)
            meta = f"semantic='{p['semantic_query']}'(where 被安全网拦截,已降级)" + cu_note
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
                # 组合查询(有 where)时纳入 plan 层(major/方向)课,让「专业范围」覆盖完整课表;
                # 纯专业课表查询仍只列直属课(via_plan='',保留原有 plan 层提示)。
                rows = program_lookup.courses_for_program(
                    conn, pid, requirement_type=req, direct_only=not p.get("where"))
                courses = [{**c, "code": c.get("course_code")} for c in rows]  # 归一化 code 键
                pick = f"(从 {len(progs)} 个匹配中选第一个)" if len(progs) > 1 else ""
                if p.get("where"):
                    # 组合查询:在该专业课表(含 plan 层)范围内再按结构化条件确定性过滤(取交集)。
                    # 用 filter_search 的行以带回 学期/学分/层级/考试 字段供前端卡片。
                    prog_codes = {c["code"] for c in courses}
                    try:
                        filtered = retrieval.filter_search(conn, p["where"])
                    except ValueError as e:
                        return {"plan": p, "mode": "empty",
                                "meta": f"非法 where 被安全网拦截:{e}", "courses": [],
                                "program_facts": None, "prog_answer": None, "chunks": []}
                    courses = [c for c in filtered if c["code"] in prog_codes]
                    program_facts = {"program": title, "requirement": req or "all",
                                     "filter": p["where"]}
                    meta = f"program='{title}'{pick} ∩ WHERE {p['where']} 命中 {len(courses)} 门"
                    prog_answer = _ans_program_filter(title, courses)
                else:
                    program_facts = {"program": title, "requirement": req or "all"}
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
            req = _requirement(question)
            meta = f"{code} 所属 program(共 {len(program_facts)}){REQ_LABEL.get(req, '')}"
            prog_answer = _ans_c2p(code, program_facts, req)
            # 禁课标注:明确禁修该课的专业
            excl_progs = program_lookup.programs_excluding(conn, code)
            if excl_progs:
                eg = excl_progs[0][1]
                prog_answer += f" 另有 {len(excl_progs)} 个专业明确禁修该课(不计学分),如 {eg}。"

    # KB 兜底:课程检索弱/空且知识库召回更强 -> 转知识库 FAQ/article。
    # - census date / 改密码 这类 FAQ 会让 courses 语义召回到低相关课(sim 0.45~0.5),
    #   不能只看「courses 是否为空」,要比 top sim(真课程问题如机器学习 top sim 高,不受影响)。
    # - 日期类(「2026 开学/census 哪天」)常被误判成 filter 且课程命中空;用高 sim 门槛转 KB,
    #   既能命中学术日历(sim≈0.66),又挡住「Gatton 校区的课」这种弱相关误转(sim≈0.6)。
    kb_chunks = None
    if mode in ("semantic", "hybrid"):
        courses_top = max((c.get("sim") or 0.0 for c in courses), default=0.0)
        if courses_top < KB_PREFER_SIM:
            cand = _kb_or_none(conn, question)
            if cand and cand[0]["sim"] > courses_top:
                kb_chunks = cand
    elif mode == "filter" and not det_answer:
        # filter 命中空,或「问的是日期(开学/census)且 where 只是时间限定(无课程筛选维度)」
        # -> 多半不是课程查询(planner 把 2026/S1 当结构化条件了),知识库召回够强就转。
        # 已有确定性 det_answer(低负担 / 非枚举值空提示)时不转 KB:KB 帮不上,且会冲掉明确提示。
        date_q = bool(_DATE_INTENT.search(question))
        only_time = not _COURSE_DIM.search(p.get("where") or "")
        if (not courses) or (date_q and only_time):
            cand = _kb_or_none(conn, question)
            if cand and cand[0]["sim"] >= KB_STRONG_SIM:
                kb_chunks = cand
    if kb_chunks:
        return {"plan": p, "mode": "kb", "meta": f"kb(课程检索弱/空转知识库;原 {meta})",
                "courses": [], "program_facts": None, "prog_answer": None, "chunks": kb_chunks}

    # 「按 code 首位数字筛年级」确定性后过滤(code 不进 SQL where,在此 Python 层裁剪首位数字)。
    levels = p.get("code_levels") or []
    if levels:
        before = len(courses)
        courses = [c for c in courses if _first_digit(c.get("code")) in set(levels)]
        meta += f" ∩ code首位∈{{{','.join(levels)}}}({before}→{len(courses)})"

    return {"plan": p, "mode": mode, "meta": meta,
            "courses": courses, "program_facts": program_facts,
            "prog_answer": prog_answer, "det_answer": det_answer, "chunks": [],
            "status_note": _status_note(conn, p.get("where"), cu, levels,
                                        both_semesters=p.get("both_semesters", False),
                                        exclude_title=p.get("exclude_title") or None)}


def _kb_or_none(conn, question: str, query_en: str | None = None) -> list:
    """知识库语义检索 + answerability 门;KB 是增强兜底层。

    query_en(planner kb 模式产出的英文 KB query)非空时,kb_search 取 max(sim_中, sim_英) 召回
    (跨语言根因修复:语料英文、中文 query 贴阈抖动);为空时单语,行为不变。answerability 门
    仍用原始中文 question 判定(虚构实体/年份校验不依赖译文)。
    检索失败(向量服务波动)只优雅降级为「无召回」;但 answerability 门判否(虚构实体/
    年份越界)是确定性拒答,return [] 让下游 KB_REFUSE 接管(同步/流式两路复用,answer.py 零改)。
    词表缺失等配置错误从 answerable() 抛出、向上传播——不混进上面的降级 except 里被静默(规则 19)。"""
    try:
        chunks = retrieval.kb_search(conn, question, min_sim=KB_SOFT_SIM, query_en=query_en)
    except Exception:
        return []
    if not chunks:
        return []
    ok, reason = answerability.answerable(question, chunks)
    if not ok:
        print(f"[answerability] 拒答:{reason} | q={question!r}")
        return []
    # P2:确定性门放行后再过 LLM 可答性门(补中文虚构实体)。LLM 抖动/故障 fail-open(放行),
    # 否则一次外部服务异常会误拒所有 KB 真问题(踩穿红线 3);仅记日志,不静默(规则 19)。
    try:
        ok2, reason2 = answerability.llm_answerable(question, chunks)
    except Exception as e:
        print(f"[answerability] LLM 门异常,fail-open 放行:{type(e).__name__}: {e} | q={question!r}")
        ok2 = True
    if not ok2:
        print(f"[answerability] LLM 拒答:{reason2} | q={question!r}")
        return []
    return chunks


# 「没有 X」三态列(midterm_status / group_status)查询时,被排除的 unknown 行的提示文案。
# col -> 提示里描述「为何这些课判不出」的短语;查询 col='none' 时确定性统计同范围的 col='unknown' 课数。
_UNKNOWN_NOTE_KINDS = {
    "midterm_status": "的考核命名无法确定是否含期中考试",
    "group_status": "没有可解析的考核数据,无法确定是否含小组/团队评估",
}


def _status_unknown_note(conn, where: str | None, col: str, coord_units=None,
                         code_levels=None, both_semesters: bool = False,
                         exclude_title=None) -> str:
    """「没有 X」(三态列 col='none')查询的确定性兜底提示(规则19:不静默漏掉无法判定的课)。

    where 命中 col='none' 时,把它换成 ='unknown' 在同等条件下统计课数。
    unknown = 判不出是否含该项,绝不计入「没有 X」,但必须告知学生自行核对。
    统计必须与主查询同范围:同样施加 coord_units(学院)与 code_levels(code 首位)过滤,
    both_semesters 时也走两学期合取统计,否则报的是全库/并集数,会把不在范围内的课算进来误导学生。
    统计失败只降级为不加提示,不抛断主流程。"""
    none_re = re.compile(rf"{col}\s*=\s*'none'", re.I)
    if not where or not none_re.search(where):
        return ""
    unk_where = none_re.sub(f"{col}='unknown'", where)
    try:
        if both_semesters:
            rows = retrieval.filter_search_both_semesters(conn, unk_where, coord_units=coord_units,
                                                          exclude_title=exclude_title)
        else:
            rows = retrieval.filter_search(conn, unk_where, coord_units=coord_units,
                                           exclude_title=exclude_title)
    except Exception:
        return ""
    levels = set(code_levels or [])
    if levels:
        rows = [c for c in rows if _first_digit(c.get("code")) in levels]
    n = len(rows)
    if n == 0:
        return ""
    return (f"\n\n注:另有 {n} 门课{_UNKNOWN_NOTE_KINDS[col]},未计入上面的名单,"
            f"请到对应课程大纲(ECP)逐一核对。")


def _status_note(conn, where, coord_units=None, code_levels=None,
                 both_semesters: bool = False, exclude_title=None) -> str:
    """汇总各三态列(midterm / group)的 unknown 兜底提示;命中几列就拼几条。"""
    return "".join(
        _status_unknown_note(conn, where, col, coord_units, code_levels,
                             both_semesters=both_semesters, exclude_title=exclude_title)
        for col in _UNKNOWN_NOTE_KINDS
    )


def run(conn, question: str, generate: bool = True) -> dict:
    r = _retrieve(conn, question)
    if r["mode"] == "empty":
        return {"plan": None, "mode": "empty", "meta": r["meta"], "courses": [],
                "program_facts": None, "chunks": [], "answer": EMPTY_MSG if generate else None}
    ans = None
    if generate:
        if r["mode"] == "kb":
            ans = answer.answer_kb(question, r["chunks"])
        elif r["mode"] == "course_detail":
            ans = answer.answer_course_detail(question, r.get("course"))
        elif r["mode"] == "program":
            ans = r["prog_answer"]
        elif r.get("det_answer"):                # 低负担/躺平:确定性答案,绕开 LLM(红线1)
            ans = r["det_answer"]
        else:
            ans = answer.answer(question, r["courses"][:ANSWER_CAP],
                                _gen_facts(r["courses"], r["program_facts"]),
                                topical=r["mode"] in ("semantic", "hybrid"))
            if ans and r.get("status_note"):    # 「没有期中/没有 group」查询确定性补 unknown 提示
                ans += r["status_note"]
    if r["mode"] == "program":                  # 确定性答案,不喂 LLM,无检索上下文
        gen_ctx: list[str] = []
    elif r["mode"] in ("kb", "course_detail"):
        gen_ctx = answer.gen_contexts(r["mode"], chunks=r.get("chunks"), course=r.get("course"),
                                      question=question)
    else:                                       # filter/semantic/hybrid:对齐生产实际入参(capped + _gen_facts)
        gen_ctx = answer.gen_contexts(
            r["mode"], courses=r["courses"][:ANSWER_CAP],
            program_facts=_gen_facts(r["courses"], r["program_facts"]))
    return {"plan": r["plan"], "mode": r["mode"], "meta": r["meta"],
            "courses": r["courses"], "program_facts": r["program_facts"],
            "chunks": r.get("chunks", []), "course": r.get("course"),
            "answer": ans, "gen_context": gen_ctx}


def run_stream(conn, question: str):
    """流式问答,依次 yield (event, data):
       ('meta', {mode, meta, courses, program_facts}) -> ('token', delta)... -> ('done', 完整答案)。
       empty 给固定兜底句;program 答案确定性(单块);其余模式逐 token 流式 + 收尾护栏。"""
    r = _retrieve(conn, question)
    mode = r["mode"]
    yield ("meta", {"mode": mode, "meta": r["meta"], "courses": r["courses"],
                    "program_facts": r["program_facts"], "chunks": r.get("chunks", []),
                    "course": r.get("course")})

    if mode == "empty":
        yield ("token", EMPTY_MSG)
        yield ("done", EMPTY_MSG)
        return
    if mode == "program":
        ans = r["prog_answer"] or ""
        yield ("token", ans)
        yield ("done", ans)
        return
    if r.get("det_answer"):                      # 低负担/躺平:确定性答案,单块发(不走 LLM,红线1)
        ans = r["det_answer"]
        yield ("token", ans)
        yield ("done", ans)
        return
    if mode == "kb":                            # 知识库:流式正文;来源走 meta.chunks,前端渲染来源卡
        chunks = r["chunks"]
        if not chunks:
            yield ("token", answer.KB_REFUSE)
            yield ("done", answer.KB_REFUSE)
            return
        fixed = answer.fixed_kb_body(chunks)    # 高风险主题(census)走确定性模板,不流 LLM
        if fixed:
            yield ("token", fixed)
            yield ("done", fixed)
            return
        acc: list[str] = []
        for delta in answer.answer_kb_stream(question, chunks):
            acc.append(delta)
            yield ("token", delta)
        full = "".join(acc)
        if answer.is_empty_kb_answer(full):     # 流式空答兜底:用与非流式一致的重试+降级覆盖 done
            full = answer.kb_answer_body(question, chunks)
        yield ("done", full)
        return
    if mode == "course_detail":                 # 单课介绍:流式简介(结构化事实走 meta.course 前端卡)
        acc: list[str] = []
        for delta in answer.answer_course_detail_stream(question, r.get("course")):
            acc.append(delta)
            yield ("token", delta)
        yield ("done", "".join(acc))
        return

    capped = r["courses"][:ANSWER_CAP]
    acc: list[str] = []
    for delta in answer.answer_stream(question, capped, _gen_facts(r["courses"], r["program_facts"]),
                                      topical=mode in ("semantic", "hybrid")):
        acc.append(delta)
        yield ("token", delta)
    full = answer.guard_citations("".join(acc), capped)
    note = r.get("status_note") or ""           # 「没有期中/没有 group」查询确定性补 unknown 提示
    if note:
        yield ("token", note)
    yield ("done", full + note)


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
