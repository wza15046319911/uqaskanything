"""deepeval_eval.py — runs DeepEval (LLM-as-judge) tiered deep evaluation on the samples produced by generate.py,
picking metrics adaptively based on "the data actually available per sample", no longer silently dropping samples without context (rule 19).

Covers the full range of queries from simple to complex (course_detail / kb / semantic / filter / hybrid / program /
refusal / broad), split and summarized by difficulty tier and mode. Metrics are chosen by the fields available per sample:
  - AnswerRelevancyMetric     runs whenever there is an answer (needs only input+output), measures whether the answer is on topic
  - FaithfulnessMetric        runs only with retrieval context, measures whether the answer is supported by the context (anti-hallucination, red line 1)
  - ContextualRelevancyMetric runs only with retrieval context, measures whether the retrieval context is relevant
  - ContextualPrecisionMetric runs only with context + reference, whether relevant context is ranked first
  - ContextualRecallMetric    runs only with context + reference, whether retrieval covers the key points of the reference answer
  - Correctness(GEval)        runs whenever there is a reference, whether the answer's facts match the reference answer
Deterministic checks (non-LLM, rule 12):
  - refuse samples   -> whether the answer matches the backend KB_REFUSE refusal wording (refusal_ok)
  - broad samples    -> whether the answer correctly narrows down (matches EMPTY_MSG) or gives a course list
Program enumeration (program) is deterministic rendering with zero hallucination, and its correctness is covered by the backend answer_eval answer_has assertion,
so here we only run AnswerRelevancy to check on-topicness, and do not send faithfulness (otherwise a zero-hallucination answer would be wrongly judged as unsupported).

Usage (needs DEEPSEEK_API_KEY in eval/.env; from the repo root, using the deepeval-specific venv):
    eval/.venv-deepeval/bin/python eval/generate.py        # produce samples first
    eval/.venv-deepeval/bin/python eval/deepeval_eval.py   # then tiered scoring -> reports/deepeval_report.{json,md}
    eval/.venv-deepeval/bin/python eval/deepeval_eval.py --limit 3   # smoke test
"""
from __future__ import annotations

import os

os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")
os.environ.setdefault("DEEPEVAL_DISABLE_PROGRESS_BAR", "YES")

import json
import argparse
from pathlib import Path
from collections import defaultdict

from deepeval.test_case import LLMTestCase, LLMTestCaseParams
from deepeval.metrics import (
    FaithfulnessMetric,
    AnswerRelevancyMetric,
    ContextualRelevancyMetric,
    ContextualPrecisionMetric,
    ContextualRecallMetric,
    GEval,
)

from deepeval_config import build_judge

HERE = Path(__file__).resolve().parent

# Stable anchors mirroring the backend answer.KB_REFUSE / qa.EMPTY_MSG (eval is decoupled from the backend process, does not import backend)
REFUSE_ANCHOR = "没找到能直接回答"
EMPTY_ANCHOR = "问题太宽泛"

METRIC_LABELS = {
    "FaithfulnessMetric": "faithfulness",
    "AnswerRelevancyMetric": "answer_relevancy",
    "ContextualRelevancyMetric": "contextual_relevancy",
    "ContextualPrecisionMetric": "contextual_precision",
    "ContextualRecallMetric": "contextual_recall",
}


def load_samples(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def build_metrics(judge) -> dict:
    """Assemble metric instances (judge=DeepSeek, serial scoring for stability), reused by key; measure() overwrites the score each time."""
    return {
        "faithfulness": FaithfulnessMetric(model=judge, async_mode=False, include_reason=True),
        "answer_relevancy": AnswerRelevancyMetric(model=judge, async_mode=False, include_reason=True),
        "contextual_relevancy": ContextualRelevancyMetric(model=judge, async_mode=False, include_reason=True),
        "contextual_precision": ContextualPrecisionMetric(model=judge, async_mode=False, include_reason=True),
        "contextual_recall": ContextualRecallMetric(model=judge, async_mode=False, include_reason=True),
        "correctness": GEval(
            name="Correctness",
            model=judge,
            evaluation_params=[
                LLMTestCaseParams.INPUT,
                LLMTestCaseParams.ACTUAL_OUTPUT,
                LLMTestCaseParams.EXPECTED_OUTPUT,
            ],
            criteria=(
                "判断 ACTUAL_OUTPUT 是否与 EXPECTED_OUTPUT 表达的核心事实一致:课程的主题/学科"
                "方向、学分、学期、先修等关键事实是否正确,有无与标准答案矛盾或编造的事实。"
                "只要核心主题与关键事实正确即视为正确;官方课程名的具体措辞、答案更详尽、换种"
                "说法都不扣分;关键事实缺失、与标准答案矛盾或编造才扣分。"
            ),
            async_mode=False,
        ),
    }


def pick_metrics(r: dict) -> list[str]:
    """Pick metrics by the fields available per sample: with an answer run relevancy; with context add faithfulness/contextual;
    with a reference add correctness, and when context is complete also add precision/recall. program has no context and no ref
    -> only answer_relevancy."""
    has_ctx = bool(r.get("contexts"))
    has_ref = bool(r.get("reference"))
    keys = ["answer_relevancy"]
    if has_ctx:
        keys += ["faithfulness", "contextual_relevancy"]
    if has_ref:
        keys.append("correctness")
        if has_ctx:
            keys += ["contextual_precision", "contextual_recall"]
    return keys


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", default=str(HERE / "data" / "generated.jsonl"))
    ap.add_argument("--out", default=str(HERE / "reports" / "deepeval_report.json"))
    ap.add_argument("--md", default=str(HERE / "reports" / "deepeval_report.md"))
    ap.add_argument("--limit", type=int, default=0, help="只评前 N 条(冒烟用,0=全部)")
    args = ap.parse_args()

    samples_path = Path(args.samples)
    if not samples_path.exists():
        raise FileNotFoundError(f"找不到样本 {samples_path};先跑 python eval/generate.py")
    samples = load_samples(samples_path)
    if args.limit:
        samples = samples[: args.limit]
    if not samples:
        raise RuntimeError("没有样本可评")

    judge = build_judge()
    M = build_metrics(judge)
    print(f"评测 {len(samples)} 条样本 (judge=DeepSeek,分层自适应指标)...")

    per_sample: list[dict] = []
    agg: dict[str, list[float]] = defaultdict(list)
    by_tier: dict = defaultdict(lambda: defaultdict(list))
    by_mode: dict = defaultdict(lambda: defaultdict(list))
    det = {"refuse": {"n": 0, "passed": 0, "fails": []},
           "broad": {"n": 0, "passed": 0, "fails": []}}
    metric_errors: list[str] = []

    for r in samples:
        q = r.get("q", "")
        answer = r.get("answer") or ""
        tier = r.get("tier")
        mode = r.get("mode")
        rec = {"q": q, "mode": mode, "tier": tier, "category": None, "scores": {}}

        # ---- Deterministic checks (non-LLM) ----
        if r.get("refuse"):
            rec["category"] = "refuse"
            ok = REFUSE_ANCHOR in answer
            det["refuse"]["n"] += 1
            det["refuse"]["passed"] += int(ok)
            rec["refusal_ok"] = ok
            if not ok:
                det["refuse"]["fails"].append(q)
            per_sample.append(rec)
            print(f"  {'✓' if ok else '✗'} [refuse] {q[:42]}")
            continue
        if r.get("broad"):
            rec["category"] = "broad"
            narrowed = EMPTY_ANCHOR in answer
            listed = bool(r.get("contexts"))
            ok = narrowed or listed                  # either a narrow-down hint or directly giving a course list counts as reasonable
            det["broad"]["n"] += 1
            det["broad"]["passed"] += int(ok)
            rec["broad_ok"] = ok
            rec["broad_behavior"] = "narrowed" if narrowed else ("listed" if listed else "other")
            if not ok:
                det["broad"]["fails"].append(q)
            per_sample.append(rec)
            print(f"  {'✓' if ok else '✗'} [broad/{rec['broad_behavior']}] {q[:36]}")
            continue

        # ---- LLM scoring (adaptive) ----
        if not answer:
            rec["category"] = "no_answer"
            rec["note"] = "后端无作答(answer 为空),跳过 LLM 指标"
            per_sample.append(rec)
            print(f"  · [no_answer] {q[:42]}")
            continue
        rec["category"] = "program" if mode == "program" else "answer"
        tc = LLMTestCase(
            input=q,
            actual_output=answer,
            retrieval_context=r.get("contexts") or None,
            expected_output=r.get("reference"),
        )
        for key in pick_metrics(r):
            metric = M[key]
            try:
                metric.measure(tc)
                score = float(metric.score) if metric.score is not None else None
                rec["scores"][key] = {"score": score, "reason": metric.reason}
                if score is not None:
                    agg[key].append(score)
                    if tier is not None:
                        by_tier[tier][key].append(score)
                    if mode:
                        by_mode[mode][key].append(score)
            except Exception as e:                    # do not swallow errors: record and continue (rule 19)
                msg = f"{q[:40]} / {key}: {type(e).__name__}: {e}"
                metric_errors.append(msg)
                rec["scores"][key] = {"score": None, "error": f"{type(e).__name__}: {e}"}
        worst = min((v["score"] for v in rec["scores"].values() if v.get("score") is not None),
                    default=None)
        per_sample.append(rec)
        ws = f" worst={worst:.2f}" if worst is not None else ""
        print(f"  ✓ [{rec['category']}/T{tier}] {q[:36]}{ws}")

    # ---- Summarize ----
    def means(d):
        return {k: round(sum(v) / len(v), 4) for k, v in d.items() if v}

    summary = means(agg)
    tier_counts = defaultdict(int)
    mode_counts = defaultdict(int)
    for rec in per_sample:
        if rec.get("scores"):
            if rec.get("tier") is not None:
                tier_counts[rec["tier"]] += 1
            if rec.get("mode"):
                mode_counts[rec["mode"]] += 1
    tier_summary = {str(t): {**means(m), "_n_scored": tier_counts.get(t, 0)}
                    for t, m in sorted(by_tier.items())}
    mode_summary = {k: {**means(m), "_n_scored": mode_counts.get(k, 0)}
                    for k, m in sorted(by_mode.items())}

    # Coverage matrix tier x mode (includes deterministic samples)
    coverage: dict = defaultdict(lambda: defaultdict(int))
    for rec in per_sample:
        t = str(rec.get("tier"))
        m = rec.get("mode") or rec.get("category")
        coverage[t][m] += 1

    # Weakest samples (lowest score on any LLM metric, ascending)
    scored_samples = [s for s in per_sample if s.get("scores") and
                      any(v.get("score") is not None for v in s["scores"].values())]
    def min_score(s):
        return min(v["score"] for v in s["scores"].values() if v.get("score") is not None)
    weakest = sorted(scored_samples, key=min_score)[:8]

    for d in ("refuse", "broad"):
        n = det[d]["n"]
        det[d]["rate"] = round(det[d]["passed"] / n, 4) if n else None

    report = {
        "n_samples": len(samples),
        "n_llm_scored": len(scored_samples),
        "summary": summary,
        "by_tier": tier_summary,
        "by_mode": mode_summary,
        "deterministic": det,
        "coverage": {t: dict(m) for t, m in coverage.items()},
        "metric_errors": metric_errors,
        "weakest": [{"q": s["q"], "mode": s["mode"], "tier": s["tier"],
                     "min_score": round(min_score(s), 4),
                     "scores": {k: v.get("score") for k, v in s["scores"].items()}}
                    for s in weakest],
        "per_sample": per_sample,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(Path(args.md), report)

    print("\n==== 汇总(均值,0-1)====")
    for k, v in summary.items():
        print(f"  {k:24s} {v:.4f}")
    print(f"\n确定性:拒答 {det['refuse']['passed']}/{det['refuse']['n']} | "
          f"宽泛 {det['broad']['passed']}/{det['broad']['n']}")
    if metric_errors:
        print(f"\n⚠ 指标执行失败 {len(metric_errors)} 处(已记入报告,未静默):")
        for m in metric_errors[:10]:
            print(f"  - {m}")
    print(f"\n报告写入 {out_path}\nMarkdown 写入 {args.md}")


def write_markdown(path: Path, rep: dict) -> None:
    METRICS = ["answer_relevancy", "faithfulness", "contextual_relevancy",
               "contextual_precision", "contextual_recall", "correctness"]
    def cell(d, k):
        v = d.get(k)
        return f"{v:.3f}" if isinstance(v, (int, float)) else "—"
    L = []
    L.append("# DeepEval 分层深度评测报告\n")
    L.append(f"- 样本总数:**{rep['n_samples']}** | LLM 判分样本:**{rep['n_llm_scored']}** "
             f"| judge:DeepSeek(temperature=0)\n")
    L.append("\n## 总体均值(0–1,越高越好)\n")
    L.append("| 指标 | 均值 |\n|---|---|")
    for k in METRICS:
        if k in rep["summary"]:
            L.append(f"| {k} | {rep['summary'][k]:.4f} |")
    L.append("\n## 确定性判定(非 LLM)\n")
    L.append("| 类别 | 通过/总数 | 通过率 |\n|---|---|---|")
    for d in ("refuse", "broad"):
        info = rep["deterministic"][d]
        rate = info.get("rate")
        L.append(f"| {d} | {info['passed']}/{info['n']} | "
                 f"{rate if rate is None else f'{rate:.0%}'} |")
    for d in ("refuse", "broad"):
        fails = rep["deterministic"][d].get("fails") or []
        if fails:
            L.append(f"\n> {d} 未通过:" + "; ".join(fails))
    L.append("\n## 按难度分层(LLM 指标均值)\n")
    hdr = "| Tier | n | " + " | ".join(METRICS) + " |"
    L.append(hdr)
    L.append("|" + "---|" * (len(METRICS) + 2))
    for t, m in rep["by_tier"].items():
        row = f"| T{t} | {m.get('_n_scored','')} | " + " | ".join(cell(m, k) for k in METRICS) + " |"
        L.append(row)
    L.append("\n## 按 mode(LLM 指标均值)\n")
    L.append(hdr.replace("Tier", "Mode"))
    L.append("|" + "---|" * (len(METRICS) + 2))
    for mode, m in rep["by_mode"].items():
        row = f"| {mode} | {m.get('_n_scored','')} | " + " | ".join(cell(m, k) for k in METRICS) + " |"
        L.append(row)
    L.append("\n## 覆盖矩阵(tier × mode,样本数)\n")
    modes = sorted({mm for t in rep["coverage"].values() for mm in t})
    L.append("| Tier | " + " | ".join(modes) + " |")
    L.append("|" + "---|" * (len(modes) + 1))
    for t in sorted(rep["coverage"]):
        cells = [str(rep["coverage"][t].get(mm, "")) for mm in modes]
        L.append(f"| T{t} | " + " | ".join(cells) + " |")
    L.append("\n## 最弱样本(任一指标最低分,升序)\n")
    L.append("| min | q | mode | 各指标分 |\n|---|---|---|---|")
    for w in rep["weakest"]:
        sc = ", ".join(f"{k}={v:.2f}" for k, v in w["scores"].items() if v is not None)
        L.append(f"| {w['min_score']:.2f} | {w['q'][:40]} | {w['mode']} | {sc} |")
    if rep["metric_errors"]:
        L.append("\n## 指标执行失败(未静默)\n")
        for e in rep["metric_errors"]:
            L.append(f"- {e}")
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
