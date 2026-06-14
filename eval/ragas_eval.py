"""ragas_eval.py — 对 generate.py 产出的样本跑 RAGAS 指标,输出每题分 + 汇总。

默认指标(都不需要 golden 答案,开箱即用):
  - faithfulness                      答案是否被检索上下文支撑(防幻觉,对应红线 1)
  - response_relevancy                答案是否切题(用 bge-m3 算相似度)
  - llm_context_precision_without_ref 检索上下文是否相关
样本若带 reference 字段,额外加 context_recall(检索是否覆盖标准答案要点)。
无检索上下文的样本(program/empty)会被剔除并计数,不混入分母(规则 19)。

用法(需 eval/.env 里的 DEEPSEEK_API_KEY + 本地 ollama;从仓库根):
    python eval/generate.py        # 先产样本
    python eval/ragas_eval.py      # 再评分
"""
from __future__ import annotations

import os
import json
import argparse
from pathlib import Path

from ragas import evaluate, EvaluationDataset
from ragas.run_config import RunConfig
from ragas.metrics import (
    Faithfulness,
    ResponseRelevancy,
    LLMContextPrecisionWithoutReference,
    LLMContextRecall,
)

from ragas_config import build_judge

HERE = Path(__file__).resolve().parent


def load_samples(path: Path) -> tuple[list[dict], list[str]]:
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    usable, dropped = [], []
    for r in rows:
        if not r.get("contexts"):
            dropped.append(f"{r.get('q')!r} (mode={r.get('mode')})")
            continue
        sample = {
            "user_input": r["q"],
            "response": r.get("answer", ""),
            "retrieved_contexts": r["contexts"],
        }
        if r.get("reference"):
            sample["reference"] = r["reference"]
        usable.append(sample)
    return usable, dropped


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", default=str(HERE / "data" / "generated.jsonl"))
    ap.add_argument("--out", default=str(HERE / "reports" / "ragas_report.json"))
    args = ap.parse_args()

    samples_path = Path(args.samples)
    if not samples_path.exists():
        raise FileNotFoundError(f"找不到样本 {samples_path};先跑 python eval/generate.py")

    samples, dropped = load_samples(samples_path)
    if dropped:
        print(f"剔除无上下文样本 {len(dropped)} 条(不计入指标):")
        for d in dropped:
            print(f"  - {d}")
    if not samples:
        raise RuntimeError("没有可评测样本(全部无检索上下文)")

    llm, emb = build_judge()
    metrics = [
        Faithfulness(),
        ResponseRelevancy(),
        LLMContextPrecisionWithoutReference(),
    ]
    if any("reference" in s for s in samples):
        metrics.append(LLMContextRecall())

    dataset = EvaluationDataset.from_list(samples)
    run_config = RunConfig(timeout=300, max_retries=3, max_wait=90, max_workers=4)
    print(f"评测 {len(samples)} 条样本 × {len(metrics)} 指标 (judge=DeepSeek, emb=bge-m3)...")
    result = evaluate(dataset=dataset, metrics=metrics, llm=llm, embeddings=emb,
                      run_config=run_config)

    df = result.to_pandas()
    print("\n==== 每题分 ====")
    print(df.to_string())
    print("\n==== 汇总(均值)====")
    for k, v in result._repr_dict.items():
        print(f"  {k}: {v:.4f}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "summary": {k: float(v) for k, v in result._repr_dict.items()},
        "n_samples": len(samples),
        "dropped": dropped,
        "per_sample": json.loads(df.to_json(orient="records", force_ascii=False)),
    }
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告写入 {out_path}")


if __name__ == "__main__":
    main()
