"""
rerank_probe.py — cross-encoder rerank 能否分开 bi-encoder 分不开的「该答 / 该拒」
(承接 threshold_scan:bge-m3 cosine 对虚构实体有不可约重叠,验证 cross-encoder 是否打破天花板)

对 kb_refuse.jsonl 每题:bge-m3 取 top-N 候选 chunk,cross-encoder 对 (query, chunk.text)
重排打分;取最高 cross-encoder 分(ce_top)与最高 bi-encoder cosine(bi_top)。对比两个
信号对 answer / refuse 的可分性(各自的重叠区与不可约误差数)。

结论用途:若 ce_top 能把虚构问题压到真问题之下(重叠消失或缩小),说明 cross-encoder
是当前拒答天花板的对症解,值得接进 kb_search;否则不接(省依赖与延迟)。

用法(需 torch + sentence-transformers + Postgres:5433 kb_chunks + Ollama bge-m3):
    python -m app.pipelines.rerank_probe
    python -m app.pipelines.rerank_probe --cand 20 --model cross-encoder/ms-marco-MiniLM-L-6-v2
"""
from __future__ import annotations
import json
import argparse
from pathlib import Path

import psycopg

from app.core.config import DSN, DATA_DIR
from app.services import retrieval

DEFAULT_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"


def _candidates(conn, query: str, cand: int) -> list[tuple[str, float]]:
    """bge-m3 取 top-cand:返回 [(chunk_text, bi_sim), ...]。"""
    vec = retrieval._embed(query)
    rows = conn.execute(
        "SELECT text, 1-(embedding<=>%s::vector) AS sim FROM kb_chunks "
        "ORDER BY embedding<=>%s::vector LIMIT %s", (vec, vec, cand)).fetchall()
    return [(r[0], float(r[1])) for r in rows]


def _separability(rows: list[dict], key: str) -> dict:
    """对某信号(key=bi/ce)做可分性分析:返回 answer 最低分、refuse 最高分、
    重叠区里的不可约误差(高分 refuse / 低分 answer)。"""
    ans = sorted((r for r in rows if r["label"] == "answer"), key=lambda r: r[key])
    ref = sorted((r for r in rows if r["label"] == "refuse"), key=lambda r: -r[key])
    ans_min = ans[0][key] if ans else 0.0
    ref_max = ref[0][key] if ref else 0.0
    overlap_ref = [r for r in ref if r[key] >= ans_min]   # refuse 但分高于某 answer
    overlap_ans = [r for r in ans if r[key] <= ref_max]   # answer 但分低于某 refuse
    # 最优单阈值准确率(在所有分值上找最佳切点)
    vals = sorted({r[key] for r in rows})
    best_acc, best_t = 0.0, None
    n = len(rows)
    for t in vals:
        a_pass = sum(r["label"] == "answer" and r[key] >= t for r in rows)
        r_block = sum(r["label"] == "refuse" and r[key] < t for r in rows)
        acc = (a_pass + r_block) / n
        if acc > best_acc:
            best_acc, best_t = acc, t
    return {"ans_min": ans_min, "ref_max": ref_max, "overlap_ref": overlap_ref,
            "overlap_ans": overlap_ans, "best_acc": best_acc, "best_t": best_t}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", default=str(DATA_DIR / "eval" / "kb_refuse.jsonl"))
    ap.add_argument("--cand", type=int, default=20, help="bge-m3 召回候选数(进重排)")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="cross-encoder 模型")
    args = ap.parse_args()

    path = Path(args.golden)
    if not path.exists():
        ap.error(f"找不到评测集 {path}")
    cases = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]

    from sentence_transformers import CrossEncoder
    print(f"加载 cross-encoder:{args.model} ...")
    ce = CrossEncoder(args.model, trust_remote_code=True)  # jina-reranker-v2 自带 modeling 代码,需此项

    rows: list[dict] = []
    print(f"评测集:{path.name} | {len(cases)} 题 | 候选 top-{args.cand} | 计算 bi/ce ...")
    with psycopg.connect(DSN) as conn:
        conn.read_only = True
        for c in cases:
            cands = _candidates(conn, c["q"], args.cand)
            bi_top = max((s for _, s in cands), default=0.0)
            scores = ce.predict([(c["q"], txt) for txt, _ in cands]) if cands else [0.0]
            ce_top = float(max(scores))
            rows.append({"q": c["q"], "label": c["label"], "bi": bi_top, "ce": ce_top})

    n_ans = sum(r["label"] == "answer" for r in rows)
    n_ref = len(rows) - n_ans
    print(f"\n=== rerank 可分性对比({len(rows)} 题:{n_ans} answer / {n_ref} refuse)===")

    for key, name in (("bi", "bi-encoder cosine (bge-m3)"), ("ce", "cross-encoder 重排分")):
        s = _separability(rows, key)
        irreducible = max(len(s["overlap_ref"]), 0)
        print(f"\n[{name}]")
        print(f"  answer 最低 = {s['ans_min']:.3f} | refuse 最高 = {s['ref_max']:.3f} | "
              f"最优单阈值准确率 = {s['best_acc']*100:.0f}% (切点 {s['best_t']:.3f})")
        if s["overlap_ref"]:
            print(f"  ⚠️ 挡不住的高分 refuse(不可约误差 {len(s['overlap_ref'])}):")
            for r in sorted(s["overlap_ref"], key=lambda r: -r[key]):
                print(f"     {key}={r[key]:.3f}  {r['q']}")
        else:
            print(f"  ✓ 无高分 refuse:任意切点 ∈ ({s['ref_max']:.3f}, {s['ans_min']:.3f}] 可 100% 分开")

    # 逐题对照(看每个虚构问题在两个信号下的相对位置)
    print(f"\n=== 逐题(bi / ce,refuse 标 *)===")
    for r in sorted(rows, key=lambda r: -r["ce"]):
        tag = "*refuse" if r["label"] == "refuse" else " answer"
        print(f"  bi={r['bi']:.3f}  ce={r['ce']:+.3f}  [{tag}] {r['q'][:42]}")


if __name__ == "__main__":
    main()
