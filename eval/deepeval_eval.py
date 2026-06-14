"""deepeval_eval.py — 对 generate.py 产出的样本跑 DeepEval(LLM-as-judge)指标,输出每题分 + 汇总。

与 ragas_eval.py 互相对照:复用同一份 eval/data/generated.jsonl(同源样本、口径一致),
换 DeepEval 的判分实现做交叉验证,抓单一 judge 框架的系统性偏差。
默认指标(都不需要 golden 答案,开箱即用):
  - FaithfulnessMetric        答案是否被检索上下文支撑(防幻觉,对应红线 1)
  - AnswerRelevancyMetric     答案是否切题
  - ContextualRelevancyMetric 检索上下文是否相关
样本带 reference 字段时,额外加(需 golden 答案):
  - ContextualPrecisionMetric 相关上下文是否排在前面
  - ContextualRecallMetric    检索是否覆盖标准答案要点
无检索上下文的样本(program/empty)会被剔除并计数,不混入分母(规则 19)。

用法(需 eval/.env 里的 DEEPSEEK_API_KEY;从仓库根,用 deepeval 专属 venv):
    eval/.venv-deepeval/bin/python eval/generate.py        # 先产样本(与 ragas 共用)
    eval/.venv-deepeval/bin/python eval/deepeval_eval.py   # 再评分
"""
from __future__ import annotations

import os

os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")
os.environ.setdefault("DEEPEVAL_DISABLE_PROGRESS_BAR", "YES")

import json
import argparse
from pathlib import Path

from deepeval.test_case import LLMTestCase
from deepeval.metrics import (
    FaithfulnessMetric,
    AnswerRelevancyMetric,
    ContextualRelevancyMetric,
    ContextualPrecisionMetric,
    ContextualRecallMetric,
)

from deepeval_config import build_judge

HERE = Path(__file__).resolve().parent


def load_samples(path: Path) -> tuple[list[dict], list[str]]:
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    usable, dropped = [], []
    for r in rows:
        if not r.get("contexts"):
            dropped.append(f"{r.get('q')!r} (mode={r.get('mode')})")
            continue
        usable.append(r)
    return usable, dropped


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", default=str(HERE / "data" / "generated.jsonl"))
    ap.add_argument("--out", default=str(HERE / "reports" / "deepeval_report.json"))
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

    judge = build_judge()
    base_metrics = [
        FaithfulnessMetric(model=judge, async_mode=False, include_reason=True),
        AnswerRelevancyMetric(model=judge, async_mode=False, include_reason=True),
        ContextualRelevancyMetric(model=judge, async_mode=False, include_reason=True),
    ]
    ref_metrics = [
        ContextualPrecisionMetric(model=judge, async_mode=False, include_reason=True),
        ContextualRecallMetric(model=judge, async_mode=False, include_reason=True),
    ]
    print(f"评测 {len(samples)} 条样本 (judge=DeepSeek)...")

    per_sample: list[dict] = []
    agg: dict[str, list[float]] = {}
    for r in samples:
        tc = LLMTestCase(
            input=r["q"],
            actual_output=r.get("answer", ""),
            retrieval_context=r["contexts"],
            expected_output=r.get("reference"),
        )
        metrics = base_metrics + (ref_metrics if r.get("reference") else [])
        scores: dict[str, dict] = {}
        for metric in metrics:
            name = metric.__class__.__name__
            metric.measure(tc)
            scores[name] = {"score": metric.score, "reason": metric.reason}
            agg.setdefault(name, []).append(metric.score)
        per_sample.append({"q": r["q"], "mode": r.get("mode"), "scores": scores})
        print(f"  ✓ {r['q'][:48]}")

    summary = {k: sum(v) / len(v) for k, v in agg.items() if v}
    print("\n==== 汇总(均值)====")
    for k, v in summary.items():
        print(f"  {k}: {v:.4f}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "summary": summary,
        "n_samples": len(samples),
        "dropped": dropped,
        "per_sample": per_sample,
    }
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告写入 {out_path}")


if __name__ == "__main__":
    main()
