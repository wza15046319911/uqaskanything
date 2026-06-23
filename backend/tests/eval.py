"""
eval.py — QA eval set + scoring (qa plan stage one: foundation)
Run qa.run over a set of gold questions and measure:
  - routing accuracy: did the planner pick the right mode (the biggest failure point)
  - filter: result set vs reference SQL ground truth (precision/recall/exact)
  - semantic/hybrid: recall@k of required course codes (retrieval quality)
  - program: is the course code / direction extracted correctly

Usage:
    python eval.py            # run all gold, print per-question + summary
    python eval.py -v         # add per-question hit details
"""
from __future__ import annotations
import os
import argparse

import psycopg

from app.services import qa

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres:uqrag@localhost:5433/uq_courses")

# gold question set. ref_sql gives the filter ground truth; must_include gives the required course codes for semantic/hybrid.
GOLD = [
    # ---- filter (structured, ground truth computed from ref_sql) ----
    {"q": "哪些课没有考试", "mode": "filter",
     "ref_sql": "SELECT code FROM courses WHERE has_exam=false"},
    {"q": "有哪些研究生课程", "mode": "filter",
     "ref_sql": "SELECT code FROM courses WHERE level='Postgraduate Coursework'"},
    {"q": "没有考试的研究生课", "mode": "filter",
     "ref_sql": "SELECT code FROM courses WHERE has_exam=false AND level='Postgraduate Coursework'"},
    {"q": "哪些课有 hurdle 要求", "mode": "filter",
     "ref_sql": "SELECT code FROM courses WHERE has_hurdle=true"},
    {"q": "有哪些2学分的课", "mode": "filter",
     "ref_sql": "SELECT code FROM courses WHERE units=2"},
    {"q": "本科有哪些有考试的课", "mode": "filter",
     "ref_sql": "SELECT code FROM courses WHERE level='Undergraduate' AND has_exam=true"},

    # ---- semantic (semantic search, must include the relevant course codes) ----
    {"q": "跟机器学习相关的课", "mode": "semantic", "must_include": ["COMP4702", "COMP7703"]},
    {"q": "找跟数据科学相关的课", "mode": "semantic", "must_include": ["DATA7001"]},
    {"q": "想了解网络安全有哪些课", "mode": "semantic", "must_include": ["CYBR7001"]},
    {"q": "跟数据库相关的课", "mode": "semantic", "must_include": ["INFS3200"]},
    {"q": "创意写作有哪些课", "mode": "semantic", "must_include": ["WRIT2050"]},
    {"q": "跟可持续发展、气候变化相关的课", "mode": "semantic", "must_include": ["ENVM3115"]},

    # ---- hybrid (structured + semantic) ----
    {"q": "CS有哪些课程没有考试", "mode": "hybrid", "must_include": ["COMP1100"],
     "must_all": "has_exam=false"},
    {"q": "研究生阶段跟数据科学相关的课", "mode": "hybrid", "must_include": ["DATA7001"],
     "must_all": "level='Postgraduate Coursework'"},
    {"q": "没有考试的、跟创意写作相关的课", "mode": "hybrid", "must_include": ["WRIT3050"],
     "must_all": "has_exam=false"},
    {"q": "计算机相关、没有hurdle的课", "mode": "hybrid", "must_all": "has_hurdle=false"},

    # ---- program (relational query, check extraction) ----
    {"q": "CSSE1001是哪些专业的必修", "mode": "program",
     "direction": "course_to_programs", "course_code": "CSSE1001"},
    {"q": "COMP3506是哪些program的课", "mode": "program",
     "direction": "course_to_programs", "course_code": "COMP3506"},
    {"q": "Bachelor of Computer Science 要修哪些核心课", "mode": "program",
     "direction": "program_to_courses"},
]


def _codes(rows, key="code"):
    return {r.get(key) for r in rows if r.get(key)}


def evaluate(conn, verbose=False):
    routing_ok = 0
    filter_exact = filter_total = 0
    sem_recall_sum = sem_total = 0
    prog_ok = prog_total = 0
    rows_out = []

    for g in GOLD:
        res = qa.run(conn, g["q"], generate=False)
        mode = res["mode"]
        mok = mode == g["mode"]
        routing_ok += mok
        detail = ""

        if g["mode"] == "filter":
            filter_total += 1
            ref = {r[0] for r in conn.execute(g["ref_sql"])}
            got = _codes(res["courses"])
            inter = ref & got
            prec = len(inter) / len(got) if got else 0.0
            rec = len(inter) / len(ref) if ref else 1.0
            exact = (ref == got)
            filter_exact += exact
            detail = f"got={len(got)} ref={len(ref)} P={prec:.2f} R={rec:.2f} {'EXACT' if exact else 'DIFF'}"

        elif g["mode"] in ("semantic", "hybrid"):
            got = _codes(res["courses"])
            if g.get("must_include"):
                sem_total += 1
                must = set(g["must_include"])
                r = len(must & got) / len(must)
                sem_recall_sum += r
                detail = f"recall(must)={r:.2f} got={sorted(got)[:4]}…"
            # hybrid structured constraint: do all hit items satisfy must_all
            if g.get("must_all") and got:
                ref = {r[0] for r in conn.execute(
                    f"SELECT code FROM courses WHERE {g['must_all']}")}
                viol = got - ref
                detail += f" | 违反{g['must_all']}={len(viol)}"

        elif g["mode"] == "program":
            prog_total += 1
            p = res["plan"]
            ok = (p.get("direction") == g["direction"])
            if g["direction"] == "course_to_programs":
                ok = ok and p.get("course_code") == g["course_code"] and bool(res["program_facts"])
            else:
                ok = ok and bool(res["courses"])
            prog_ok += ok
            detail = f"dir={p.get('direction')} code={p.get('course_code')} ok={ok}"

        rows_out.append((g["q"], g["mode"], mode, mok, detail))

    # print
    print(f"{'问题':<28} {'期望':<9} {'实际':<9} {'路由':<4} 详情")
    print("-" * 100)
    for q, exp, act, mok, det in rows_out:
        print(f"{q[:26]:<28} {exp:<9} {act:<9} {'✓' if mok else '✗':<4} {det}")

    n = len(GOLD)
    print("\n=== 汇总 ===")
    print(f"路由准确率:     {routing_ok}/{n} = {routing_ok/n:.0%}")
    if filter_total:
        print(f"filter 精确匹配: {filter_exact}/{filter_total} = {filter_exact/filter_total:.0%}")
    if sem_total:
        print(f"语义必含 recall: {sem_recall_sum/sem_total:.0%}(平均,n={sem_total})")
    if prog_total:
        print(f"program 抽取正确: {prog_ok}/{prog_total} = {prog_ok/prog_total:.0%}")


def regression_checks(conn) -> bool:
    """Regression assertions for adversarial-review fixes (one assert each, locking in fixed bugs)."""
    from app.services import retrieval
    from app.services import answer
    from app.services import planner
    checks = []

    # 1. Enum guard: a non-St Lucia campus must never return the whole DB
    n = len(qa.run(conn, "Gatton 校区有哪些课", generate=False)["courses"])
    checks.append(("枚举守卫:Gatton 不返回全库", n < 100, f"命中 {n} 门(应≈0,绝不≈1508)"))

    # 2. Elective tri-state routing
    r = qa.run(conn, "Bachelor of Computer Science 有哪些选修课", generate=False)
    checks.append(("选修三态路由", "选修" in r["meta"], f"meta={r['meta']}"))

    # 3. Broad question falls back gracefully (no traceback raised)
    try:
        m = qa.run(conn, "随便推荐几门课", generate=False)["mode"]
        checks.append(("宽泛问题优雅兜底", True, f"mode={m}"))
    except Exception as e:
        checks.append(("宽泛问题优雅兜底", False, f"抛异常 {e}"))

    # 4. Injection safety (structural): build_where puts all values into params (never into SQL text), _validate_filters drops hallucinated columns
    inj = "St Lucia'; DROP TABLE courses --"
    sql, params = retrieval.build_where({"location": inj})
    safe = "%s" in sql and inj in params and inj not in sql
    dropped = planner._validate_filters({"requirement_type": "x", "title": "%ml%"}) == {}
    checks.append(("注入安全(参数化+丢脑补列)", safe and dropped, f"sql={sql!r}"))

    # 5. Answer guardrail: out-of-scope (invented) course codes are removed, valid codes kept (split by line, avoid removing the whole line)
    out = answer.guard_citations("推荐 COMP4702。\n还有 FAKE9999。", [{"code": "COMP4702"}])
    head = out.split("[警告]")[0]
    checks.append(("答案护栏剔除越界码", "FAKE9999" not in head and "COMP4702" in head, f"out={out[:50]!r}"))

    # 6. Course code extraction next to CJK characters
    p = qa.run(conn, "CSSE1001是哪些专业的必修", generate=False)["plan"]
    checks.append(("CJK相邻课码抽取", bool(p) and p.get("course_code") == "CSSE1001",
                   f"code={p and p.get('course_code')}"))

    from app.services import program_lookup

    # 7. Equivalence collapse (bypass planner, deterministic): BCompSc core = 12 slots / 2 groups, answer renders "MATH1061 or MATH1081"
    rows = program_lookup.courses_for_program(conn, "2559", requirement_type="core")
    norm = [{**r, "code": r.get("course_code")} for r in rows]
    slots = qa._collapse_slots(norm)
    ngrp = sum(1 for s in slots if s["is_group"])
    ans = qa._ans_p2c("Bachelor of Computer Science", "core", norm)
    checks.append(("二选一折叠:BCompSc核心=12槽/2组",
                   len(slots) == 12 and ngrp == 2 and "二选一" in ans and "MATH1061 或 MATH1081" in ans,
                   f"槽={len(slots)} 组={ngrp}"))

    # 8. A choose-one core must not be reported as mandatory (bypass planner): MATH1061 in BCompSc (2559) should be core+equiv_group + labelled "choose-one core"
    facts: dict = {}
    for r in program_lookup.programs_for_course(conn, "MATH1061"):
        pid = r["program_id"]
        if pid not in facts or qa._c2p_rank(r) < qa._c2p_rank(facts[pid]):
            facts[pid] = r
    pf = list(facts.values())
    ans = qa._ans_c2p("MATH1061", pf)
    bcs = [f for f in pf if f["program_id"] == "2559"]
    checks.append(("MATH1061→BCompSc 为二选一核心",
                   bool(bcs) and bcs[0]["requirement_type"] == "core"
                   and bool(bcs[0].get("equiv_group")) and "二选一核心" in ans,
                   f"bcs_equiv={bcs[0].get('equiv_group') if bcs else None}"))

    # 9. Multi-option group wording is correct (no longer hardcoded "choose two"): 2033 has a choose-one-of-three group (HHSS6020/6030/6040)
    rows = program_lookup.courses_for_program(conn, "2033", requirement_type="core")
    ans = qa._ans_p2c("Bachelor of Social Science (Honours)", "core",
                      [{**r, "code": r.get("course_code")} for r in rows])
    checks.append(("多选项组措辞正确(非硬编码二选一)",
                   "HHSS6020 或 HHSS6030 或 HHSS6040" in ans and "二选一" not in ans,
                   f"ans={ans[:70]!r}"))

    # 10. A: force select-part into core — 2033 (Honours) core is no longer empty, = 2 choose-one slots
    rows = program_lookup.courses_for_program(conn, "2033", requirement_type="core")
    slots = qa._collapse_slots([{**r, "code": r.get("course_code")} for r in rows])
    checks.append(("A:Honours强制select归核心(2033=2槽)",
                   len(slots) == 2 and all(s["is_group"] for s in slots),
                   f"核心槽={len(slots)}"))

    # 11. B: plan-level core hint gating — True for a program with a major, False for an Honours without a major
    checks.append(("B:plan层核心门控正确",
                   program_lookup.has_plan_level_core(conn, "2561") is True
                   and program_lookup.has_plan_level_core(conn, "2033") is False,
                   f"2561={program_lookup.has_plan_level_core(conn, '2561')} 2033={program_lookup.has_plan_level_core(conn, '2033')}"))

    # 12. C: deterministic program forcing (including dual-degree plural names) + does not break normal course queries
    f1 = planner._force_program_route("Bachelors of Mathematics / Computer Science 要修哪些核心课")
    f2 = planner._force_program_route("CSSE1001是哪些专业的必修")
    f3 = planner._force_program_route("CS有哪些课程没有考试")
    checks.append(("C:program强制(双学位名)+不误伤",
                   bool(f1) and f1[0] == "program_to_courses" and "Mathematics" in f1[2]
                   and bool(f2) and f2[0] == "course_to_programs" and f2[1] == "CSSE1001"
                   and f3 is None,
                   f"f1={f1} f2={f2} f3={f3}"))

    # 13. C: postgraduate/undergraduate -> deterministically inject the level slot (filters dict)
    checks.append(("C:研究生→level过滤注入",
                   planner._enforce_level_hint({}, "研究生阶段跟数据科学相关的课") == {"level": "Postgraduate Coursework"}
                   and planner._enforce_level_hint({"has_exam": True}, "本科有考试的课") == {"has_exam": True, "level": "Undergraduate"},
                   "ok"))

    # 14. Program-level banned course: BCompSc (2559) bans MATH1040; permit routing + answer "cannot"
    ex2559 = program_lookup.excluded_courses(conn, "2559")
    f = planner._force_program_route("Bachelor of Computer Science 能修 MATH1040 吗")
    permit = qa._ans_permit("MATH1040", "Bachelor of Computer Science",
                            program_lookup.is_excluded(conn, "2559", "MATH1040"), [])
    checks.append(("程序级禁课:CS禁MATH1040 + permit路由",
                   "MATH1040" in ex2559 and bool(f) and f[0] == "permit" and f[1] == "MATH1040"
                   and permit.startswith("不能"),
                   f"ex2559={ex2559} forced={f}"))

    # 15. permit does not over-trigger: course code + degree but no "can I take" keyword -> does not go to permit
    f2 = planner._force_program_route("CSSE1001 在 Bachelor of Computer Science 是必修吗")
    checks.append(("permit不误伤(无能否修关键词)",
                   f2 is None or f2[0] != "permit", f"forced={f2}"))

    print("\n=== 回归断言(对抗修复)===")
    allok = True
    for name, ok, det in checks:
        print(f"  {'✓' if ok else '✗'} {name}: {det}")
        allok = allok and ok
    print(f"回归断言:{sum(1 for _,o,_ in checks if o)}/{len(checks)} 通过")
    return allok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    from app.services import retrieval
    with psycopg.connect(DSN) as conn:
        retrieval.ensure_fts_index(conn)        # read path no longer builds the index; build it once at startup
        evaluate(conn, args.verbose)
        regression_checks(conn)


if __name__ == "__main__":
    main()
