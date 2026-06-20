"""
llm_judge_eval.py — lightweight LLM-as-judge fuzzy quality eval (RAGAS faithfulness/relevance idea, zero external deps)

Deterministic evals (route_eval / answer_eval) can only assert "is the route right, is the course
code in the set, did it refuse", and cannot answer fuzzy quality like "is this LLM-generated answer
on topic and grounded". Here we reuse this project's LLM backend (DeepSeek/local) as the judge,
scoring each qa.run answer on two 1-5 dimensions:
  - relevance: whether the answer is on topic and directly responds to the question
  - faithfulness: whether the factual statements in the answer are all supported by the "retrieval
    context" (no fabrication); a low score if the context is empty but specific facts are given.
    Equivalent to RAGAS answer_relevancy + faithfulness, but with the project's own LLM, no ragas install.

Only "answered" questions are judged (course/kb etc.); refuse / empty / non-enum deterministic notes
are counted separately and not sent to the judge. The judge is LLM scoring, non-deterministic, so
scores drift a little; use it to see trends and locate low-score questions, not as a bit-exact regression.

Usage (run from backend/, needs Postgres:5433 + KB loaded + an LLM backend ready):
    python -m app.pipelines.llm_judge_eval
    python -m app.pipelines.llm_judge_eval --golden data/eval/answers.jsonl --limit 25 --show
"""
from __future__ import annotations
import json
import argparse
from pathlib import Path

import psycopg

from app.core.config import DSN, DATA_DIR
from app.services import qa, answer, llm

_JUDGE = """你是严格的问答质量评审。只依据下面给出的【问题】【答案】【检索依据】打分,
绝不用你自己的外部知识替答案补全或脑补。按 1-5 打分(5 最好,1 最差):
- relevance:答案是否切题、直接回应了【问题】(跑题/答非所问给低分)。
- faithfulness:答案里的事实性陈述(课程码、专业、日期、流程等)是否都能在【检索依据】里
  找到支撑,有没有编造。若【检索依据】为空但答案仍给出具体事实,faithfulness 给低分。
只输出 JSON,不要解释:{{"relevance": <1-5>, "faithfulness": <1-5>, "issues": "<一句话扣分点,没有则空字符串>"}}

【问题】{q}

【答案】{a}

【检索依据】
{ctx}"""


def _context_str(res: dict) -> str:
    """Build the answer's factual context into a judge-readable string. course/kb use gen_context;
    for deterministic answers like program / low-load gen_context is empty (the answer is rendered
    deterministically in code from DB structured data, not the LLM), so add back program_facts/courses
    as the context -- otherwise the judge would misjudge a "zero-hallucination deterministic answer"
    as "ungrounded fabrication" (a faithfulness false positive)."""
    ctx = res.get("gen_context") or []
    parts = [str(c) for c in ctx if c] if isinstance(ctx, list) else [str(ctx)]
    if res.get("program_facts"):
        parts.append(f"program_facts={res['program_facts']}")
    if res.get("courses"):
        parts.append("courses=" + "; ".join(
            f"{c.get('code')}({c.get('title')})" for c in res["courses"][:40]))
    joined = "\n".join(p for p in parts if p).strip()
    return joined[:4000] if joined else "(无检索依据)"


def _judge(q: str, ans: str, ctx: str) -> dict:
    """Run one LLM judge pass, return {relevance, faithfulness, issues}; raise ValueError on parse failure (not silent)."""
    raw = llm.call([{"role": "user", "content": _JUDGE.format(q=q, a=ans, ctx=ctx)}],
                   json_mode=True)
    try:
        o = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"评审返回非法 JSON:{raw!r}") from e
    return {"relevance": int(o.get("relevance", 0)),
            "faithfulness": int(o.get("faithfulness", 0)),
            "issues": str(o.get("issues", "") or "")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", default=str(DATA_DIR / "eval" / "answers.jsonl"))
    ap.add_argument("--limit", type=int, default=0, help="最多评多少题(0=全部),控时长")
    ap.add_argument("--show", action="store_true", help="打印每题分数")
    ap.add_argument("--floor", type=int, default=3, help="低分阈值:任一项 < floor 即标记")
    args = ap.parse_args()

    path = Path(args.golden)
    if not path.exists():
        ap.error(f"找不到评测集 {path}")
    cases = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.limit:
        cases = cases[:args.limit]

    scored: list[tuple[str, dict]] = []
    skipped = 0          # refuse / empty / not answered, not sent to the judge
    print(f"[backend={llm.backend_name()}] 评测集:{path.name} | {len(cases)} 题(LLM-judge)\n")
    with psycopg.connect(DSN) as conn:
        conn.read_only = True
        for c in cases:
            q = c["q"]
            res = qa.run(conn, q, generate=True)
            ans = res.get("answer") or ""
            if not ans or answer.is_kb_refuse(ans) or res.get("mode") == "empty":
                skipped += 1
                continue
            v = _judge(q, ans, _context_str(res))
            scored.append((q, v))
            if args.show:
                print(f"  R={v['relevance']} F={v['faithfulness']}  {q}"
                      + (f"  ⚠ {v['issues']}" if v['issues'] else ""))

    n = len(scored)
    if not n:
        print("没有可评审的作答题(全部拒答/empty)。")
        return
    mr = sum(v["relevance"] for _, v in scored) / n
    mf = sum(v["faithfulness"] for _, v in scored) / n
    low = [(q, v) for q, v in scored if v["relevance"] < args.floor or v["faithfulness"] < args.floor]

    print(f"\n=== LLM-judge 质量评测(作答 {n} 题,跳过拒答/empty {skipped} 题)===")
    print(f"平均 relevance:    {mr:.2f}/5")
    print(f"平均 faithfulness: {mf:.2f}/5")
    print(f"低分题(任一项 < {args.floor}):{len(low)}")
    for q, v in low:
        print(f"  [R={v['relevance']} F={v['faithfulness']}] {q}  | {v['issues']}")


if __name__ == "__main__":
    main()
