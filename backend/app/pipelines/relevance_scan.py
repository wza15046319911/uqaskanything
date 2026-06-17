"""
relevance_scan.py — course topic relevance eval (regression guard for P0 "course retrieval relevance ceiling")

Run qa.run per question for an end-to-end answer, then assert two topic types deterministically:
  - expect=relevant: the corpus really has courses on this topic. Must be mode in {semantic,hybrid},
    a non-empty answer with no "no strong match" honesty note, and the returned course set must hit
    at least one expected code (any of codes is enough, loose to avoid brittleness).
  - expect=no_strong_match: the corpus has no course really on this topic (e.g. game dev / crypto).
    The system must never confidently list them as "X courses" -- pass conditions: carry a
    "no strong match found" honesty note, or mode=empty / empty answer / no recall.

Also print top sim per question: to show plainly that "absolute sim cannot separate" -- a real
low-score topic (statistics 0.556) and an empty topic (game design 0.550) score almost the same,
so a pure-threshold scheme is rejected in favour of answer's relevance-honesty instruction (LLM
classification) as the fallback.

Note: the answer body is LLM-generated and the relevance-honesty note is an LLM classification
result, non-deterministic, so the pass rate drifts a little; use it to see trends and locate
regressions, not as a bit-exact regression (same as answer_eval / llm_judge_eval).

Usage (run from backend/, needs Postgres:5433 + an LLM backend ready):
    python -m app.pipelines.relevance_scan
    python -m app.pipelines.relevance_scan --golden data/eval/course_relevance.jsonl --show-ok
"""
from __future__ import annotations
import json
import argparse
from pathlib import Path

import psycopg

from app.core.config import DSN, DATA_DIR
from app.services import qa, answer


def _has_no_match_caveat(ans: str) -> bool:
    """Whether the answer carries an honest fallback note like "no strong match found with X /
    please verify yourself / semantically closest"."""
    if "请自行甄别" in ans or "语义最接近" in ans:
        return True
    return "强相关" in ans and ("未找到" in ans or "未能找到" in ans or "没有找到" in ans)


def _top_sim(res: dict) -> float | None:
    """Highest vector similarity of the returned courses in the end-to-end result (semantic/hybrid
    rows carry sim); return None if not available."""
    for c in res.get("courses") or []:
        if "sim" in c:
            return float(c["sim"])
    return None


def _codes_in(res: dict, codes: list[str]) -> list[str]:
    """The expected course codes that actually appear in the returned course set (any hit counts as a recall success)."""
    got = {c.get("code") for c in (res.get("courses") or [])}
    return [c for c in codes if c in got]


def _check(exp: dict, res: dict) -> list[str]:
    """Run deterministic assertions on a single topic query, return a list of failure reasons (empty = pass)."""
    fails: list[str] = []
    ans = res.get("answer") or ""
    mode = res.get("mode")
    caveat = _has_no_match_caveat(ans)

    if exp["expect"] == "relevant":
        if mode not in ("semantic", "hybrid"):
            fails.append(f"真实主题路由到 {mode}(期望 semantic/hybrid)")
        if not ans or ans == answer.EMPTY_ANSWER or mode == "empty":
            fails.append("真实主题却空答/empty")
        if caveat:
            fails.append("真实主题被误判「无强相关」(诚实声明误触发)")
        hit = _codes_in(res, exp.get("codes", []))
        if exp.get("codes") and not hit:
            fails.append(f"期望课码一个都没召回:{exp['codes']}")
    else:  # no_strong_match
        listed = bool(res.get("courses"))
        confidently_listed = (mode in ("semantic", "hybrid")
                              and listed and not caveat
                              and ans and ans != answer.EMPTY_ANSWER)
        if confidently_listed:
            fails.append("空主题被自信当成「X 课」列出(无诚实声明)")
    return fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", default=str(DATA_DIR / "eval" / "course_relevance.jsonl"),
                    help="评测集 JSONL(每行 {q, topic, expect, codes?})")
    ap.add_argument("--show-ok", action="store_true", help="同时打印通过的用例")
    args = ap.parse_args()

    path = Path(args.golden)
    if not path.exists():
        ap.error(f"找不到评测集 {path}")
    cases = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not cases:
        ap.error(f"评测集为空:{path}")

    rel_ok = rel_n = empty_ok = empty_n = 0
    failures: list[tuple[str, str]] = []
    rel_top_sims: list[float] = []      # real-topic top sim (passing ones)
    empty_top_sims: list[float] = []    # empty-topic top sim
    print(f"评测集:{path.name} | {len(cases)} 题(端到端,逐题跑 qa.run)\n")
    print(f"{'topic':14s} {'expect':16s} {'mode':9s} {'top_sim':8s} {'caveat':7s} 结果")
    with psycopg.connect(DSN) as conn:
        conn.read_only = True
        for exp in cases:
            res = qa.run(conn, exp["q"], generate=True)
            fails = _check(exp, res)
            ts = _top_sim(res)
            caveat = _has_no_match_caveat(res.get("answer") or "")
            if exp["expect"] == "relevant":
                rel_n += 1
                rel_ok += not fails
                if not fails and ts is not None:
                    rel_top_sims.append(ts)
            else:
                empty_n += 1
                empty_ok += not fails
                if ts is not None:
                    empty_top_sims.append(ts)
            mark = "✓" if not fails else "✗ " + "; ".join(fails)
            ts_str = f"{ts:.3f}" if ts is not None else "  -  "
            if fails or args.show_ok:
                print(f"{exp['topic']:14s} {exp['expect']:16s} {str(res['mode']):9s} "
                      f"{ts_str:8s} {'是' if caveat else '否':7s} {mark}")
            if fails:
                failures.append((exp["topic"], "; ".join(fails)))

    print(f"\n=== 课程主题相关性评测 ===")
    print(f"真实主题(应相关):     {rel_ok}/{rel_n} 通过")
    print(f"空主题(应无强相关):   {empty_ok}/{empty_n} 通过")
    # show plainly that "absolute sim cannot separate": real-topic lowest top sim vs empty-topic highest top sim must overlap
    if rel_top_sims and empty_top_sims:
        print(f"\nsim 重叠证据(故不用纯阈值):")
        print(f"  真实主题最低 top_sim = {min(rel_top_sims):.3f}")
        print(f"  空主题最高 top_sim   = {max(empty_top_sims):.3f}")
        if max(empty_top_sims) >= min(rel_top_sims):
            print(f"  → 空主题最高分 ≥ 真实主题最低分:任何纯 sim 阈值都会误伤,改由相关性诚实指令裁定。")
    if failures:
        print(f"\n失败({len(failures)}):")
        for topic, why in failures:
            print(f"  [✗] {topic}  | {why}")
    else:
        print("\n全部通过 ✓")


if __name__ == "__main__":
    main()
