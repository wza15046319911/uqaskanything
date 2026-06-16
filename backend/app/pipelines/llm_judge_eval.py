"""
llm_judge_eval.py — 轻量 LLM-as-judge 模糊质量评测(RAGAS faithfulness/relevance 思路,零外部依赖)

确定性评测(route_eval / answer_eval)只能断言「路由对不对、课码在不在集合内、拒没拒」,
答不了「这条 LLM 生成的答案切不切题、有没有据」这类模糊质量。这里复用本项目 LLM 后端
(DeepSeek/本地)当评审,对逐题 qa.run 的答案打两项 1-5 分:
  - relevance:答案是否切题、直接回应问题
  - faithfulness:答案里的事实陈述是否都能在「检索依据」里找到支撑(无编造);依据为空却给具体
    事实则低分。等价于 RAGAS 的 answer_relevancy + faithfulness,但用项目自带 LLM,不装 ragas。

只评「作答」的题(course/kb 等);拒答 / empty / 非枚举值确定性提示单独计数,不送评审。
评审是 LLM 打分,非确定性,分数会小幅波动;看趋势与定位低分题,不是逐位回归。

用法(从 backend/ 跑,需 Postgres:5433 + KB 已灌 + LLM 后端就绪):
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
    """把答案的事实依据拼成评审可读串。course/kb 用 gen_context;program / 低负载等确定性答案
    gen_context 为空(答案在代码里由 DB 结构化数据确定性渲染,非 LLM),补回 program_facts/courses
    当依据——否则评审会把「零幻觉的确定性答案」误判成「无依据编造」(faithfulness 假阳性)。"""
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
    """跑一次 LLM 评审,返回 {relevance, faithfulness, issues};解析失败抛 ValueError(不静默)。"""
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
    skipped = 0          # 拒答 / empty / 非作答,不送评审
    print(f"[backend={llm.backend_name()}] 评测集:{path.name} | {len(cases)} 题(LLM-judge)\n")
    with psycopg.connect(DSN) as conn:
        conn.read_only = True
        for c in cases:
            q = c["q"]
            res = qa.run(conn, q, generate=True)
            ans = res.get("answer") or ""
            if not ans or ans == answer.KB_REFUSE or res.get("mode") == "empty":
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
