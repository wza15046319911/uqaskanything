"""
route_eval.py — planner routing accuracy eval (the first lever in "improve QA accuracy": routing)

Run planner.plan() for a set of real questions labelled with the expected mode/direction, compare
routing correctness per question, print accuracy grouped by expected mode and the mode misjudgement
confusion (expected -> actual), and list each error case to help add rules.

The eval target is only the planner's classification decision: mode, direction (program type), where
key conditions, and whether semantic_query is given. It does not include the qa-layer similarity-based
KB fallback switch (that is threshold tuning, evaluated separately). Only the fields given in golden are
checked (meaningful, not brittle): where_has checks a column-name substring, where_equals checks a
"column=value" substring (whitespace ignored); the latter catches the case where the value is semantically
flipped but the column name is still there (e.g. "online" -> In Person).

Note: the LLM backend is non-deterministic, so accuracy may drift a little across runs; use it to see
trends and locate error cases, not as a bit-exact regression.

Usage (run from backend/, needs Postgres:5433 + an LLM backend ready):
    python -m app.pipelines.route_eval
    python -m app.pipelines.route_eval --golden data/eval/routing.jsonl --show-ok
"""
from __future__ import annotations
import json
import argparse
from pathlib import Path

import psycopg

from app.core.config import DSN, DATA_DIR
from app.services import planner, retrieval


def _route_of(question: str, schema: str, conn) -> tuple[str, str, str, bool]:
    """Run planner.plan, return (mode, direction, where description, whether semantic was given);
    if plan raises ValueError (cannot form a retrieval condition) record it as mode='empty'.
    The where description is rendered by retrieval.describe_where from the filters slots (the readable
    dual of build_where), so the column-name/value assertions of where_has/where_equals still hold
    verbatim against the slot-based plan."""
    try:
        p = planner.plan(question, schema_doc=schema, conn=conn)
    except ValueError:
        return ("empty", "", "", False)
    return (p["mode"], p.get("direction", ""), retrieval.describe_where(p.get("filters")),
            bool(p.get("semantic_query")))


def _check(exp: dict, mode: str, direction: str, where: str, has_sem: bool) -> tuple[bool, list[str]]:
    """Compare field by field, only check the fields given in golden; return (all correct?, [failure reasons...])."""
    fails: list[str] = []
    if mode != exp["mode"]:
        fails.append(f"mode {mode}≠{exp['mode']}")
    if exp.get("direction") and direction != exp["direction"]:
        fails.append(f"direction {direction or '∅'}≠{exp['direction']}")
    for sub in exp.get("where_has", []):
        if sub.lower() not in where.lower():
            fails.append(f"where 缺 '{sub}'(实际 {where or '∅'})")
    # where_equals: check the "value" rather than just the column name appearing (whitespace ignored).
    # where_has checking only the column name would let through a semantically flipped value
    # (e.g. "online" written as attendance_mode='In Person'); value-sensitive cases use this to catch it.
    for sub in exp.get("where_equals", []):
        if sub.replace(" ", "").lower() not in where.replace(" ", "").lower():
            fails.append(f"where 值不符,缺 '{sub}'(实际 {where or '∅'})")
    if "semantic" in exp and has_sem != exp["semantic"]:
        fails.append(f"semantic={'有' if has_sem else '无'},期望{'有' if exp['semantic'] else '无'}")
    return (not fails, fails)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", default=str(DATA_DIR / "eval" / "routing.jsonl"),
                    help="评测集 JSONL(每行 {q, mode, direction?, where_has?, semantic?})")
    ap.add_argument("--show-ok", action="store_true", help="同时打印通过的用例")
    args = ap.parse_args()

    path = Path(args.golden)
    if not path.exists():
        ap.error(f"找不到评测集 {path}")
    cases = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not cases:
        ap.error(f"评测集为空:{path}")

    n = len(cases)
    full_ok = mode_ok = 0
    by_mode: dict[str, list[int]] = {}                 # expected mode -> [overall correct count, total]
    confusion: dict[tuple[str, str], int] = {}         # (expected mode, actual mode) -> count
    failures: list[tuple[str, str, str]] = []

    print(f"[backend={planner.llm.backend_name()}] 评测集:{path.name} | {n} 题\n")
    with psycopg.connect(DSN) as conn:
        conn.read_only = True
        schema = planner.build_schema_doc(conn)
        for exp in cases:
            mode, direction, where, has_sem = _route_of(exp["q"], schema, conn)
            ok, fails = _check(exp, mode, direction, where, has_sem)
            full_ok += ok
            mode_ok += (mode == exp["mode"])
            slot = by_mode.setdefault(exp["mode"], [0, 0])
            slot[0] += ok
            slot[1] += 1
            if mode != exp["mode"]:
                confusion[(exp["mode"], mode)] = confusion.get((exp["mode"], mode), 0) + 1
            if not ok:
                failures.append((exp["q"], exp["mode"], "; ".join(fails)))
            elif args.show_ok:
                tail = f"/{direction}" if direction else ""
                print(f"[✓] {exp['q']}  -> {mode}{tail}")

    print(f"\n=== 路由评测({n} 题)===")
    print(f"整体正确(mode + 关键字段全对):{full_ok}/{n} ({full_ok / n * 100:.0f}%)")
    print(f"mode 正确率:                    {mode_ok}/{n} ({mode_ok / n * 100:.0f}%)")
    print("\n按期望 mode 分组(整体正确数/总数):")
    for m in sorted(by_mode):
        c, t = by_mode[m]
        print(f"  {m:<14} {c}/{t}")
    if confusion:
        print("\nmode 误判(期望 -> 实际):")
        for (exp_m, got_m), cnt in sorted(confusion.items(), key=lambda kv: -kv[1]):
            print(f"  {exp_m} -> {got_m} : {cnt}")
    if failures:
        print(f"\n错例({len(failures)}):")
        for q, em, why in failures:
            print(f"  [✗] {q}  | 期望 {em} | {why}")
    else:
        print("\n全部通过 ✓")


if __name__ == "__main__":
    main()
