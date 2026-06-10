"""
sim_advise.py — 阶段四:AI 选课建议(确定性定池,LLM 只排序/解释)

原则:「能选什么」由引擎决定——枚举可选(available)∪ 开放规则(E/F)可计入的码;
LLM 只在这个固定候选集内排序和说明,永远不能引入集合外的码:
  护栏① 候选喂 LLM 前已按合法池过滤;
  护栏② answer.guard_citations 把 LLM 输出里集合外的码整行剥除并显式列出。
诚实披露:可选池里没有 courses 行(无 embedding,检索不可达)的码数量显式返回,
不在可达子集上静默装作覆盖完整。

用法:
    python sim_advise.py "我想做 AI 和机器学习"          # 2559 空状态自测
"""
from __future__ import annotations
import re

import answer
import llm
import retrieval
from simulator import PlanSimulator

MAX_CANDIDATES = 8
MIN_SIM = 0.45            # semantic_search 默认地板;召回 <3 时降到 0.30 重试一次
MIN_SIM_RETRY = 0.30

SYSTEM = (
    "你是 UQ 选课助手。只能从给定候选课程里挑选和排序,绝不能提到候选之外的课程码。"
    "用简体中文,按推荐度排序,每门一行:课程码 课名 —— 一句话理由(贴合学生目标)。"
    "最多推荐 5 门。没有合适的就说明原因。"
)


def _legal_target(sim, st: dict, prog_list: set, enum_ref: dict, code: str) -> str | None:
    """候选码的归属:枚举可选 -> 其规则 ref;否则按 E(程序课表)→ F(任意)找有余量的
    开放规则(undergraduate 课表的限 level<=6);无处可归 -> None(不进候选)。"""
    if code in enum_ref:
        return enum_ref[code]
    m = re.search(r"\d", code)
    lvl = int(m.group()) if m else None
    for e in st.values():
        if not e.get("open") or e.get("inactive"):
            continue
        if (e["units_max"] or 0) - e["units_counted"] <= 0:
            continue
        if e["open_scope"] == "program" and code not in prog_list:
            continue
        if e.get("open_max_level") and lvl is not None and lvl > e["open_max_level"]:
            continue
        return e["ref"]
    return None


def advise(conn, program_id: str, goal: str, selected: list = (),
           chosen_plans: list = (), branch: list = (), generate: bool = True) -> dict:
    if not goal or not goal.strip():
        raise ValueError("goal 不能为空")
    sim = PlanSimulator(conn, program_id)
    for c in selected:
        sim.select(c)
    for p in chosen_plans:
        sim.choose_plan(p)
    for ref in branch:
        sim.choose_branch(ref)

    st = {e["ref"]: e for e in sim.status()}
    enum_ref: dict[str, str] = {}            # 枚举可选码 -> 所属规则 ref
    for ref, slots in sim.available_by_rule().items():
        for s in slots:
            for code in ([s["code"]] if s["kind"] == "course" else s["options"]):
                enum_ref.setdefault(code, ref)
    prog_list = sim._all_referenced_codes()

    # 诚实披露:枚举可选里没有 courses 行的码,检索永远不可达
    unreachable = sorted(c for c in enum_ref if c not in sim._all_codes)

    open_room = any(e.get("open") and not e.get("inactive")
                    and (e["units_max"] or 0) - e["units_counted"] > 0
                    for e in st.values())
    if not enum_ref and not open_room:
        return {"goal": goal, "candidates": [], "advice": None,
                "available_count": 0, "unreachable_count": len(unreachable),
                "unreachable_codes": unreachable, "note": "可选池为空,未调用 LLM"}

    rows = retrieval.semantic_search(conn, goal, k=40, min_sim=MIN_SIM)
    cands: list[dict] = []

    def collect(rows):
        for r in rows:
            code = r["code"]
            if code in sim.selected or code in sim.excluded:
                continue
            if any(c["code"] == code for c in cands):
                continue
            ref = _legal_target(sim, st, prog_list, enum_ref, code)
            if ref is None:
                continue
            cands.append({"code": code, "title": r.get("title"),
                          "units": r.get("units"), "level": r.get("level"),
                          "semester": r.get("semester"), "sim": r.get("sim"),
                          "counts_into": ref})
            if len(cands) >= MAX_CANDIDATES:
                return

    collect(rows)
    if len(cands) < 3:                        # 召回不足:降相似度地板重试一次
        collect(retrieval.semantic_search(conn, goal, k=40, min_sim=MIN_SIM_RETRY))

    out = {"goal": goal, "candidates": cands,
           "available_count": len(enum_ref),
           "unreachable_count": len(unreachable), "unreachable_codes": unreachable,
           "advice": None}
    if not cands:
        out["note"] = "目标检索不到合法候选,未调用 LLM"
        return out
    if not generate:
        return out

    facts = "\n".join(
        f"- {c['code']} {c['title'] or ''}({c['units']}u, level {c['level']}, "
        f"计入规则 {c['counts_into']}, 相关度 {c['sim']:.2f})" for c in cands)
    raw = llm.call([
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"学生目标:{goal}\n\n候选课程(只能从这里挑):\n{facts}"},
    ], temperature=0)
    out["advice"] = answer.guard_citations(raw, cands)
    return out


if __name__ == "__main__":
    import json
    import sys

    import psycopg
    from simulator import DSN

    goal = sys.argv[1] if len(sys.argv) > 1 else "我想做 AI 和机器学习"
    with psycopg.connect(DSN) as conn:
        conn.read_only = True
        res = advise(conn, "2559", goal)
        print(json.dumps({k: v for k, v in res.items() if k != "advice"},
                         ensure_ascii=False, indent=2))
        print("\n--- advice ---\n", res["advice"])
