"""
threshold_scan.py — KB 拒答门槛(kb_search min_sim)数据驱动扫描
(对应「提高问答准确率」item 3:把手调魔数换成有数据支撑的值;student-facing 红线 3 refuse over wrong)

对一组标注 answer / refuse 的问题,**预计算**每题的 kb_top_sim(query 向量与 kb_chunks
的最高余弦相似度,与阈值无关,只算一次),再在 min_sim 网格上扫描,报告:
  - 每个阈值的答全率(answer 题被放行)/ 拒对率(refuse 题被挡)/ 综合准确率
  - 最优 min_sim(综合准确率最高;并列取更高/更保守的值)
  - 当前生产值 0.55 的表现对比
  - 「阈值分不开」的重叠区:sim 高于某些 answer 题的 refuse 题 —— 不可约误差,
    说明纯阈值有天花板,需 answerability/rerank 才能进一步分开(诚实标注,不假装能调好)

注意:只扫拒答门槛(min_sim)。转 KB 门槛(KB_PREFER_SIM / KB_STRONG_SIM)需课程侧
sim 一起建模,属后续。LLM 无关,纯向量相似度,可重复。

用法(从 backend/ 跑,需 Postgres:5433 kb_chunks 已灌 + Ollama bge-m3):
    python -m app.pipelines.threshold_scan
    python -m app.pipelines.threshold_scan --lo 0.45 --hi 0.75 --step 0.01
"""
from __future__ import annotations
import json
import argparse
from pathlib import Path

import psycopg

from app.core.config import DSN, DATA_DIR
from app.services import retrieval

PROD_MIN_SIM = 0.55  # 当前 retrieval.kb_search 的生产值(qa.py 注释里手调来的)


def _kb_top_sim(conn, query: str) -> float:
    """query 向量与 kb_chunks 的最高余弦相似度(raw,不卡任何阈值)。"""
    vec = retrieval._embed(query)
    row = conn.execute(
        "SELECT 1-(embedding<=>%s::vector) AS sim FROM kb_chunks "
        "ORDER BY embedding<=>%s::vector LIMIT 1", (vec, vec)).fetchone()
    return float(row[0]) if row else 0.0


def _accuracy(rows: list[dict], min_sim: float) -> tuple[int, int, int, int]:
    """给定阈值,返回 (answer 放行数, answer 总数, refuse 挡住数, refuse 总数)。"""
    a_pass = a_tot = r_block = r_tot = 0
    for r in rows:
        if r["label"] == "answer":
            a_tot += 1
            a_pass += r["sim"] >= min_sim
        else:
            r_tot += 1
            r_block += r["sim"] < min_sim
    return a_pass, a_tot, r_block, r_tot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", default=str(DATA_DIR / "eval" / "kb_refuse.jsonl"))
    ap.add_argument("--lo", type=float, default=0.45, help="min_sim 扫描下界")
    ap.add_argument("--hi", type=float, default=0.75, help="min_sim 扫描上界")
    ap.add_argument("--step", type=float, default=0.01, help="扫描步长")
    args = ap.parse_args()

    path = Path(args.golden)
    if not path.exists():
        ap.error(f"找不到评测集 {path}")
    cases = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not cases:
        ap.error(f"评测集为空:{path}")

    rows: list[dict] = []
    print(f"评测集:{path.name} | {len(cases)} 题 | 预计算 kb_top_sim ...")
    with psycopg.connect(DSN) as conn:
        conn.read_only = True
        for c in cases:
            sim = _kb_top_sim(conn, c["q"])
            rows.append({"q": c["q"], "label": c["label"], "sim": sim})

    n = len(rows)
    n_ans = sum(r["label"] == "answer" for r in rows)
    n_ref = n - n_ans

    # 扫描
    grid = []
    t = args.lo
    while t <= args.hi + 1e-9:
        a_pass, a_tot, r_block, r_tot = _accuracy(rows, t)
        acc = (a_pass + r_block) / n
        grid.append((round(t, 4), a_pass, a_tot, r_block, r_tot, acc))
        t += args.step
    best_acc = max(g[5] for g in grid)
    best = [g for g in grid if g[5] == best_acc]
    best_hi = best[-1]  # 并列取更高(更保守:更倾向拒答)的阈值

    # 当前生产值对比
    pa, pat, pb, prt = _accuracy(rows, PROD_MIN_SIM)
    prod_acc = (pa + pb) / n

    print(f"\n=== 拒答门槛 min_sim 扫描({n} 题:{n_ans} answer / {n_ref} refuse)===")
    print(f"当前生产值 {PROD_MIN_SIM}:  答全 {pa}/{pat} | 拒对 {pb}/{prt} | 综合 {prod_acc*100:.0f}%")
    print(f"最优 min_sim {best_hi[0]}:  答全 {best_hi[1]}/{best_hi[2]} | "
          f"拒对 {best_hi[3]}/{best_hi[4]} | 综合 {best_hi[5]*100:.0f}%"
          + (f"  (并列最优 {len(best)} 个,取最高)" if len(best) > 1 else ""))

    # 关键阈值附近一段表格(看趋势)
    print("\nmin_sim | 答全 | 拒对 | 综合")
    for thr, a_pass, a_tot, r_block, r_tot, acc in grid:
        if abs(thr * 100 % 5) < 1e-6 or thr in (best_hi[0], PROD_MIN_SIM):  # 每 0.05 + 关键点
            mark = "  <- 最优" if thr == best_hi[0] else ("  <- 生产" if thr == PROD_MIN_SIM else "")
            print(f"  {thr:.2f}  | {a_pass:>2}/{a_tot} | {r_block:>2}/{r_tot} | {acc*100:>3.0f}%{mark}")

    # 可分性:answer 最低 sim vs refuse 最高 sim;重叠 = 不可约误差
    ans_sorted = sorted((r for r in rows if r["label"] == "answer"), key=lambda r: r["sim"])
    ref_sorted = sorted((r for r in rows if r["label"] == "refuse"), key=lambda r: -r["sim"])
    ans_min = ans_sorted[0]["sim"] if ans_sorted else 0.0
    ref_max = ref_sorted[0]["sim"] if ref_sorted else 0.0
    print(f"\nanswer 最低 sim = {ans_min:.3f}({ans_sorted[0]['q'][:30]})")
    print(f"refuse 最高 sim = {ref_max:.3f}({ref_sorted[0]['q'][:30]})")
    if ref_max >= ans_min:
        # 重叠:落在 [ans_min, ref_max] 的两类题,任何单一阈值都无法同时分对
        overlap_ref = [r for r in rows if r["label"] == "refuse" and r["sim"] >= ans_min]
        overlap_ans = [r for r in rows if r["label"] == "answer" and r["sim"] <= ref_max]
        print(f"\n⚠️ 重叠区 [{ans_min:.3f}, {ref_max:.3f}]:任何单一 min_sim 都分不开 —— 不可约误差")
        print(f"   高 sim 的 refuse 题(阈值挡不住,会被编答):")
        for r in overlap_ref:
            print(f"     sim={r['sim']:.3f}  {r['q']}")
        print(f"   低 sim 的 answer 题(阈值若提高会误拒):")
        for r in sorted(overlap_ans, key=lambda r: r["sim"]):
            print(f"     sim={r['sim']:.3f}  {r['q']}")
        print("   -> 纯阈值有天花板,进一步分开需 answerability 校验 / rerank。")
    else:
        print(f"\n✓ 两类完全可分:任意 min_sim ∈ ({ref_max:.3f}, {ans_min:.3f}] 都能 100% 分对。")


if __name__ == "__main__":
    main()
