"""
answerability_eval.py — KB 拒答门评测(student-facing 红线 3 的确定性校验)
(对应 .claude/plans/kb-answerability.md P0 第 4 步)

复用 data/eval/kb_refuse.jsonl(16 answer / 8 refuse),跑生产 KB 兜底链路
(retrieval.kb_search 阈值 -> answerability.answerable 门),逐题判 refuse / answer,并标出
是哪一道挡的(低相似度阈值 / 年份越界 / 英文实体缺席),给两条硬判据:

  - **误拒 = 0(红线)**:answer 题被拒一个都不行——过不了不 ship。
  - **漏网**:refuse 题被放过的数量。尽量小;确定性门治不了的中文「半相关虚构」(火星 /
    太空站)会漏,逐条列出,作为是否上 P2 LLM gate 的依据(plan 说明此为预期天花板)。

注意:纯向量相似度 + 词表查表,无 LLM,可重复(同库同 Ollama 下结果稳定)。

用法(从 backend/ 跑,需 Postgres:5433 kb_chunks 已灌 + Ollama bge-m3 + kb_vocab.txt 已建):
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
    """跑生产兜底链路,返回 (refused, 原因)。refused=True 表示该问会拒答。"""
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

    # 词表缺失要在跑评测前就 fail loud,而不是逐题抛(规则 19)
    answerability.load_vocab()

    wrong_refuse: list[tuple[str, str]] = []   # answer 题被误拒(红线)
    leaked: list[tuple[str, str]] = []         # refuse 题被放过(漏网)
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
