"""
sim_advise.py — stage 4: AI course advice (deterministic candidate pool, LLM only ranks/explains)

Principle: "what you can pick" is decided by the engine — enumerated available ∪ codes that open rules (E/F) can count;
the LLM only ranks and explains within this fixed candidate set, and can never introduce a code outside the set:
  guardrail 1: candidates are filtered by the legal pool before feeding the LLM;
  guardrail 2: answer.guard_citations strips whole lines for out-of-set codes in the LLM output and lists them explicitly.
Honest disclosure: the number of codes in the available pool that have no courses row (no embedding, unreachable by retrieval) is returned explicitly,
not silently pretending full coverage over the reachable subset.

Usage:
    python sim_advise.py "我想做 AI 和机器学习"          # 2559 empty-state self-test
"""
from __future__ import annotations
import re

from app.services import answer, llm, retrieval, planner
from app.services.simulator import PlanSimulator

MAX_CANDIDATES = 8
MIN_SIM = 0.45            # semantic_search default floor; lower to 0.30 and retry once when recall <3
MIN_SIM_RETRY = 0.30

SYSTEM = (
    "你是 UQ 选课助手。只能从给定候选课程里挑选和排序,绝不能提到候选之外的课程码。"
    "用简体中文,按推荐度排序,每门一行:课程码 课名 —— 一句话理由(贴合学生目标)。"
    "考核事实(有无考试/小组等)以系统给定为准,绝不臆测或编造;不确定就不要提该属性。"
    "最多推荐 5 门。没有合适的就说明原因。"
)


def _goal_constraint_desc(goal: str) -> str:
    """Join the structured constraints deterministically detected in the goal (exam/group/level/units/excluded type) into a Chinese description,
    so the prompt can tell the LLM "candidates already truly satisfy these conditions". Reuses planner's extractors (rule 15: do not rebuild)."""
    parts: list[str] = []
    ex = planner._exam_intent(goal)
    if ex is False:
        parts.append("无考试")
    elif ex is True:
        parts.append("有考试")
    gr = planner._group_intent(goal)
    if gr is False:
        parts.append("无小组评估")
    elif gr is True:
        parts.append("有小组评估")
    for rx, val in planner._LEVEL_KW:
        if rx.search(goal):
            parts.append("研究生课" if val.startswith("Post") else "本科课")
            break
    mu = planner.UNITS_RE.search(goal)
    if mu:
        parts.append(f"{mu.group(1)} 学分")
    types = planner._excluded_types(goal)
    if types:
        parts.append("不含 " + "/".join(types))
    return "、".join(parts)


def _legal_target(sim, st: dict, prog_list: set, enum_ref: dict, code: str) -> str | None:
    """Attribution of a candidate code: enumerated available -> its rule ref; otherwise look for an open rule with room
    in order E (program course list) -> F (any) (undergraduate course list limited to level<=6); nowhere to go -> None (not a candidate)."""
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
    enum_ref: dict[str, str] = {}            # enumerated available code -> its rule ref
    for ref, slots in sim.available_by_rule().items():
        for s in slots:
            for code in ([s["code"]] if s["kind"] == "course" else s["options"]):
                enum_ref.setdefault(code, ref)
    prog_list = sim._all_referenced_codes()

    # honest disclosure: codes in enumerated available with no courses row are forever unreachable by retrieval
    unreachable = sorted(c for c in enum_ref if c not in sim._all_codes)

    open_room = any(e.get("open") and not e.get("inactive")
                    and (e["units_max"] or 0) - e["units_counted"] > 0
                    for e in st.values())
    if not enum_ref and not open_room:
        return {"goal": goal, "candidates": [], "advice": None,
                "available_count": 0, "unreachable_count": len(unreachable),
                "unreachable_codes": unreachable, "note": "可选池为空,未调用 LLM"}

    # deterministically detect structured constraints in the goal (exam/group/units/level/excluded type), reusing planner's controlled
    # filters generator. Red line 1 / rule 12: these are structured facts, never let the LLM guess — it would treat a "project-like" course
    # as "no exam" and recommend it (e.g. once said the exam-bearing RELN1000 had few exams). If a constraint is detected, filter the candidate pool deterministically.
    filters = planner._program_filter_where(goal)
    has_topic = planner._has_topic(goal)
    valid_codes: set | None = None
    if filters:
        try:
            valid_codes = {r["code"] for r in retrieval.filter_search(conn, filters)}
        except ValueError:                    # controlled-generated filters should not be illegal; fall back without blocking, log it
            print(f"[sim_advise] 结构化 filters 被安全网拦截,降级为不过滤: {filters!r}")
            filters = {}
    constraint = _goal_constraint_desc(goal)
    cands: list[dict] = []

    def collect(rows):
        for r in rows:
            code = r["code"]
            if code in sim.selected or code in sim.excluded:
                continue
            if valid_codes is not None and code not in valid_codes:
                continue                       # deterministic filter: drop any course not satisfying the structured constraint
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

    # candidate source: with a topic (or no structured constraint), use semantic recall + the deterministic filter above; a pure structured goal (no topic,
    # e.g. "I want courses with no exam") goes straight to deterministic filter_search for the pool that truly satisfies the conditions, not guessing by semantics.
    if has_topic or not filters:
        collect(retrieval.semantic_search(conn, goal, k=40, min_sim=MIN_SIM))
        if len(cands) < 3:                    # recall too low: lower the similarity floor and retry once (semantic path only)
            collect(retrieval.semantic_search(conn, goal, k=40, min_sim=MIN_SIM_RETRY))
    else:
        collect(retrieval.filter_search(conn, filters))

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
        f"计入规则 {c['counts_into']}"
        + (f", 相关度 {c['sim']:.2f}" if c.get("sim") is not None else "") + ")"
        for c in cands)
    user = f"学生目标:{goal}\n\n候选课程(只能从这里挑):\n{facts}"
    if constraint:
        user += (f"\n\n这些候选已由系统按确定性数据筛选,均满足「{constraint}」;"
                 f"请据此说明,不要臆测或质疑其考核情况。")
    raw = llm.call([
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": user},
    ], temperature=0)
    out["advice"] = answer.guard_citations(raw, cands)
    return out


if __name__ == "__main__":
    import json
    import sys

    import psycopg
    from app.services.simulator import DSN

    goal = sys.argv[1] if len(sys.argv) > 1 else "我想做 AI 和机器学习"
    with psycopg.connect(DSN) as conn:
        conn.read_only = True
        res = advise(conn, "2559", goal)
        print(json.dumps({k: v for k, v in res.items() if k != "advice"},
                         ensure_ascii=False, indent=2))
        print("\n--- advice ---\n", res["advice"])
