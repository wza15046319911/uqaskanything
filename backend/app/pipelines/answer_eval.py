"""
answer_eval.py — end-to-end answer correctness eval (deterministic check of student-facing red lines 1/2/3)

Run qa.run(conn, q) per question for the final answer, then make deterministic assertions (no LLM-judge):
  - source_has: the answer must carry the expected official source (URL fragment or "source" block, red line 2)
  - answer_has / answer_hasnt: the answer must contain / must not contain the expected course code / program / key fact
  - refuse=true: an out-of-scope high-risk question must refuse (KB_REFUSE) or be empty, never make things up (red line 3)
  - courses_satisfy: courses hit by filter must row-by-row really satisfy the structured condition
    (e.g. {"has_exam": false}), not just checking the route is right but verifying the returned
    course rows themselves are correct (answer quality)
  - program scope (program_to_courses auto-check): the course codes in the answer must all belong to
    that program (verified independently against program_course), never mixing in another program's
    courses -- asking for BCS program courses must not answer with another program's courses (red line 1)
  - citation safety (auto-checked for all course modes, no labelling needed): the course codes appearing
    in the answer body must all be in the retrieval result set; out-of-bounds (likely made-up) codes must
    not leak to the user (red line 1, verifies guard_citations is really wired into the production path)

Note: the answer body is LLM-generated; this eval only asserts deterministic properties (source / course
code set / refuse), not wording quality. The LLM backend is non-deterministic so the pass rate drifts a
little; use it to see trends and locate regressions, not as a bit-exact regression.

Usage (run from backend/, needs Postgres:5433 + KB loaded + an LLM backend ready):
    python -m app.pipelines.answer_eval
    python -m app.pipelines.answer_eval --golden data/eval/answers.jsonl --show-ok
"""
from __future__ import annotations
import json
import re
import argparse
from pathlib import Path

import psycopg

from app.core.config import DSN, DATA_DIR
from app.services import qa, answer, program_lookup

_CODE_RE = re.compile(r"\b[A-Z]{4}\d{4}\b")
# guard_citations' warning line marker, either language (zh "[警告]" / en "[Warning]")
_WARN_SPLIT = re.compile(r"\[警告\]|\[Warning\]")


def _leaked_codes(ans: str, courses: list[dict]) -> set[str]:
    """Course codes appearing in the answer body (warning line removed) but not in the retrieval result
    set -- they must never leak to the user."""
    allowed = {c.get("code") for c in courses if c.get("code")}
    body = _WARN_SPLIT.split(ans)[0]          # guard_citations' warning line lists the removed out-of-bounds codes; exclude it
    return {m for m in _CODE_RE.findall(body) if m not in allowed}


def _program_scope_leak(conn, res: dict) -> set[str]:
    """Course codes appearing in a program_to_courses answer but not belonging to that program (nor its
    excluded courses) -- another program's courses mixed in. Verify independently against the program_course
    table as ground truth, do not trust the answer-building logic (red line 1: answering the wrong program is wrong)."""
    plan = res.get("plan") or {}
    if plan.get("direction") != "program_to_courses":
        return set()
    progs = program_lookup.find_program(conn, plan.get("program_name") or "")
    if not progs:
        return set()
    pid = progs[0][0]
    member = {r[0] for r in conn.execute(
        "SELECT course_code FROM program_course WHERE program_id = %s", (pid,)).fetchall()}
    allowed = member | set(program_lookup.excluded_courses(conn, pid))
    body = _WARN_SPLIT.split(res.get("answer") or "")[0]
    return {m for m in _CODE_RE.findall(body) if m not in allowed}


def _check(exp: dict, res: dict, conn) -> list[str]:
    """Run deterministic assertions on a single end-to-end result, return a list of failure reasons (empty = pass)."""
    fails: list[str] = []
    ans = res.get("answer") or ""
    mode = res.get("mode")

    if "mode" in exp and mode != exp["mode"]:
        fails.append(f"mode {mode}≠{exp['mode']}")

    if exp.get("refuse"):
        if not (answer.is_kb_refuse(ans) or mode == "empty"):
            fails.append(f"应拒答但给了答案(mode={mode}):{ans[:40]}…")

    if exp.get("source_has") and exp["source_has"] not in ans:
        fails.append(f"答案缺期望来源 '{exp['source_has']}'")

    for sub in exp.get("answer_has", []):
        if sub not in ans:
            fails.append(f"答案缺 '{sub}'")

    for sub in exp.get("answer_hasnt", []):
        if sub in ans:
            fails.append(f"答案含不应出现的 '{sub}'")

    # answer quality: courses hit by filter must really satisfy the structured condition (check the returned rows row-by-row, not just the mode).
    for field, val in (exp.get("courses_satisfy") or {}).items():
        bad = [c.get("code") for c in (res.get("courses") or []) if c.get(field) != val]
        if bad:
            fails.append(f"{len(bad)} 门课不满足 {field}={val}:{'、'.join(b for b in bad[:5] if b)}")

    # answer quality: a program_to_courses answer must never mix in another program's courses (verify independently against program_course).
    if mode == "program":
        leaked = _program_scope_leak(conn, res)
        if leaked:
            fails.append(f"答案含非本专业课码:{'、'.join(sorted(leaked))}")

    # citation safety only matters for "LLM-generated, grounded in courses" modes; program's deterministic
    # answer already contains the course code the user asked about (data is in program_facts), and
    # course_detail/kb do not fall back on courses either.
    if mode in ("filter", "semantic", "hybrid"):
        leaked = _leaked_codes(ans, res.get("courses") or [])
        if leaked:
            fails.append(f"泄漏越界课码:{'、'.join(sorted(leaked))}")

    return fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", default=str(DATA_DIR / "eval" / "answers.jsonl"),
                    help="评测集 JSONL(每行 {q, mode?, source_has?, answer_has?, refuse?})")
    ap.add_argument("--show-ok", action="store_true", help="同时打印通过的用例")
    args = ap.parse_args()

    path = Path(args.golden)
    if not path.exists():
        ap.error(f"找不到评测集 {path}")
    cases = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not cases:
        ap.error(f"评测集为空:{path}")

    n = len(cases)
    ok = 0
    failures: list[tuple[str, str]] = []
    print(f"评测集:{path.name} | {n} 题(端到端,逐题跑 qa.run)\n")
    with psycopg.connect(DSN) as conn:
        conn.read_only = True
        for exp in cases:
            res = qa.run(conn, exp["q"], generate=True)
            fails = _check(exp, res, conn)
            if fails:
                failures.append((exp["q"], "; ".join(fails)))
            elif args.show_ok:
                print(f"[✓] {exp['q']}  -> {res['mode']}")
            ok += not fails

    print(f"\n=== 端到端答案评测({n} 题)===")
    print(f"通过:{ok}/{n} ({ok / n * 100:.0f}%)")
    if failures:
        print(f"\n失败({len(failures)}):")
        for q, why in failures:
            print(f"  [✗] {q}  | {why}")
    else:
        print("\n全部通过 ✓")


if __name__ == "__main__":
    main()
