"""
answerability_eval.py — KB refuse gate eval (deterministic check of student-facing red line 3)
(maps to .claude/plans/kb-answerability.md P0 step 4)

Reuse data/eval/kb_refuse.jsonl (16 answer / 8 refuse), run the production KB fallback chain
(retrieval.kb_search threshold -> answerability.answerable gate), judge refuse / answer per question,
and mark which one blocked it (low-similarity threshold / year out of bounds / English entity absent),
giving two hard criteria:

  - **wrong refuse = 0 (red line)**: not a single answer question may be refused -- if it fails, do not ship.
  - **leaked**: the number of refuse questions let through. Keep it as small as possible; the Chinese
    "half-relevant made-up" cases the deterministic gate cannot handle (Mars / space station) will leak;
    list them one by one as the basis for whether to add the P2 LLM gate (the plan says this is the expected ceiling).

Note: pure vector similarity + vocab lookup, no LLM, repeatable (results are stable under the same DB and Ollama).

Usage (run from backend/, needs Postgres:5433 with kb_chunks loaded + Ollama bge-m3 + kb_vocab.txt built):
    python -m app.pipelines.answerability_eval
    python -m app.pipelines.answerability_eval --golden data/eval/kb_refuse.jsonl
"""
from __future__ import annotations
import json
import argparse
from pathlib import Path

import psycopg

from app.core.config import DSN, DATA_DIR
from app.services import retrieval, answerability


def _decide(conn, question: str) -> tuple[bool, str]:
    """Run the production fallback chain, return (refused, reason). refused=True means this question will be refused."""
    chunks = retrieval.kb_search(conn, question)
    if not chunks:
        return True, "低相似度(min_sim 阈值)"
    ok, reason = answerability.answerable(question, chunks)
    if not ok:
        return True, reason
    return False, f"放行 sim={chunks[0]['sim']:.3f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", default=str(DATA_DIR / "eval" / "kb_refuse.jsonl"))
    args = ap.parse_args()

    path = Path(args.golden)
    if not path.exists():
        ap.error(f"找不到评测集 {path}")
    cases = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not cases:
        ap.error(f"评测集为空:{path}")

    # a missing vocab must fail loud before the eval runs, not be raised per question (rule 19)
    answerability.load_vocab()

    wrong_refuse: list[tuple[str, str]] = []   # answer questions wrongly refused (red line)
    leaked: list[tuple[str, str]] = []         # refuse questions let through (leaked)
    rows: list[tuple[str, str, bool, str]] = []  # (label, q, refused, reason)

    with psycopg.connect(DSN) as conn:
        conn.read_only = True
        for c in cases:
            refused, reason = _decide(conn, c["q"])
            rows.append((c["label"], c["q"], refused, reason))
            if c["label"] == "answer" and refused:
                wrong_refuse.append((c["q"], reason))
            elif c["label"] == "refuse" and not refused:
                leaked.append((c["q"], reason))

    n = len(rows)
    n_ans = sum(r[0] == "answer" for r in rows)
    n_ref = n - n_ans
    refused_ref = n_ref - len(leaked)

    print(f"评测集:{path.name} | {n} 题({n_ans} answer / {n_ref} refuse)\n")
    for label, q, refused, reason in rows:
        verdict = "拒答" if refused else "作答"
        bad = label == "answer" and refused
        miss = label == "refuse" and not refused
        flag = "  ✗误拒" if bad else ("  ✗漏网" if miss else "")
        print(f"  [{label:6}->{verdict}] {q}\n            ({reason}){flag}")

    print(f"\n=== answerability 拒答门评测 ===")
    print(f"虚构拒对:{refused_ref}/{n_ref}  |  真问题误拒:{len(wrong_refuse)}/{n_ans}")
    if wrong_refuse:
        print(f"\n✗ 误拒真问题(红线,过不了不 ship):")
        for q, why in wrong_refuse:
            print(f"    {q}  | {why}")
    if leaked:
        print(f"\n⚠️ 漏网虚构(确定性门的预期天花板,留 P2):")
        for q, why in leaked:
            print(f"    {q}  | {why}")

    red_line_ok = not wrong_refuse
    print(f"\n红线(误拒=0):{'通过 ✓' if red_line_ok else '未过 ✗'}"
          f"  | 虚构全拒:{'是 ✓' if refused_ref == n_ref else f'否(漏 {len(leaked)})'}")


if __name__ == "__main__":
    main()
