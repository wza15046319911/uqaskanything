"""
floor_scan.py — data-driven scan for the course semantic recall floor (SEMANTIC_MIN_SIM)
(maps to P2 "relevance floor labelled set + scan": replace the hand-set 0.50 with a
 data-backed, re-scannable value; same shape as threshold_scan (KB refuse gate), LLM-free,
 pure vector similarity, repeatable)

Based on the two label types in course_relevance.jsonl, the embed input is the English
semantic_query the planner really produces (faithful to production: course retrieval goes
"Chinese topic -> English query -> vector"; embedding the Chinese directly gives a different
sim distribution):
  - expect=relevant + codes: the corpus really has courses on this topic. The highest cosine
    between each expected course code and the query vector is the "signal"; raising the floor
    blocks it out (real hits are lost).
  - expect=no_strong_match: the corpus has no course really on this topic. For these questions
    every course the semantic nearest-neighbour (semantic_search) returns is off-topic "noise";
    a higher floor cuts away more.

Precompute (floor-independent, done once):
  - signal sim: max(1-cos) per expected course code (take the highest across semesters for the
    same course, matching the per-code dedup in _fused_search); expected codes not found in the
    DB are counted and reported explicitly, not silenced (red line / rule 19).
  - noise sim: the raw vector sim of courses returned by semantic_search(min_sim=0, k) for
    no_strong_match questions.
Then scan over a floor grid and report, for each candidate floor, the real-hit keep rate /
noise cut rate / combined, marking:
  - the current production value (retrieval.SEMANTIC_MIN_SIM) performance
  - "max-keep floor": the highest floor that still keeps all real hits (= just below the lowest
    signal sim); only a floor at or below this loses no real course. The combined-accuracy best
    value is for reference only -- it sacrifices real courses to cut more noise, and the
    remaining noise is already covered by answer's relevance-honesty instruction (approach-3),
    not by this floor (so the floor's goal is "keep real hits, cut noise as a bonus").
  - separability: lowest signal sim vs highest noise sim; overlap = irreducible error
    (quantifies the P0 conclusion: pure sim cannot split real vs empty topics).

Usage (run from backend/, needs Postgres:5433 with courses embedding loaded + Ollama bge-m3):
    python -m app.pipelines.floor_scan
    python -m app.pipelines.floor_scan --lo 0.40 --hi 0.65 --step 0.01 --show
"""
from __future__ import annotations
import json
import argparse
from pathlib import Path

import psycopg

from app.core.config import DSN, DATA_DIR
from app.services import retrieval

PROD_MIN_SIM = retrieval.SEMANTIC_MIN_SIM  # current production value (single authority, not hard-coded)


def _code_max_sim(conn, vec, code: str) -> float | None:
    """Highest cosine between an expected course code and the query vector (take the highest
    across semesters for the same course); return None if the code is not found in the DB."""
    row = conn.execute(
        "SELECT max(1-(embedding<=>%s::vector)) FROM courses "
        "WHERE code=%s AND embedding IS NOT NULL", (vec, code)).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def _query_en(case: dict) -> str:
    """Get the English query used to retrieve this question; raise explicitly if semantic_query
    is missing (the label set must carry it, the scan does not call the LLM)."""
    eq = (case.get("semantic_query") or "").strip()
    if not eq:
        raise ValueError(f"标注缺 semantic_query(扫描需英文 query,不在此调 planner):{case.get('q')!r}")
    return eq


def _precompute(conn, cases: list[dict], k: int) -> tuple[list[dict], list[dict], list[str]]:
    """Precompute signal / noise sim and the expected codes not found. Return (signals, noises, missing_codes)."""
    signals: list[dict] = []   # {topic, code, sim}
    noises: list[dict] = []     # {topic, code, sim}
    missing: list[str] = []
    for c in cases:
        vec = retrieval._embed(_query_en(c))
        if c["expect"] == "relevant":
            for code in c.get("codes", []):
                sim = _code_max_sim(conn, vec, code)
                if sim is None:
                    missing.append(f"{c['topic']}:{code}")
                    continue
                signals.append({"topic": c["topic"], "code": code, "sim": sim})
        elif c["expect"] == "no_strong_match":
            for r in retrieval.semantic_search(conn, _query_en(c), k=k, min_sim=0.0):
                noises.append({"topic": c["topic"], "code": r["code"], "sim": r["sim"]})
    return signals, noises, missing


def _rates(signals: list[dict], noises: list[dict], floor: float) -> tuple[int, int, int, int]:
    """Given a floor: return (signals kept, total signals, noise cut, total noise)."""
    kept = sum(s["sim"] >= floor for s in signals)
    cut = sum(n["sim"] < floor for n in noises)
    return kept, len(signals), cut, len(noises)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", default=str(DATA_DIR / "eval" / "course_relevance.jsonl"))
    ap.add_argument("--lo", type=float, default=0.40, help="floor 扫描下界")
    ap.add_argument("--hi", type=float, default=0.65, help="floor 扫描上界")
    ap.add_argument("--step", type=float, default=0.01, help="扫描步长")
    ap.add_argument("--k", type=int, default=8, help="噪声召回深度(对齐生产 semantic top-k)")
    ap.add_argument("--show", action="store_true", help="打印每个候选 floor(否则只打印每 0.05 + 关键点)")
    args = ap.parse_args()

    path = Path(args.golden)
    if not path.exists():
        ap.error(f"找不到评测集 {path}")
    cases = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not cases:
        ap.error(f"评测集为空:{path}")

    n_rel = sum(c["expect"] == "relevant" for c in cases)
    n_emp = sum(c["expect"] == "no_strong_match" for c in cases)
    print(f"评测集:{path.name} | {len(cases)} 题({n_rel} relevant / {n_emp} no_strong_match)"
          f" | 预计算信号/噪声 sim(英文 query,k={args.k})...")
    with psycopg.connect(DSN) as conn:
        conn.read_only = True
        signals, noises, missing = _precompute(conn, cases, args.k)

    if missing:
        # labelled expected codes not found in the DB: report explicitly, do not swallow silently
        # (otherwise the signal total is falsely low and the keep rate falsely high)
        print(f"\n⚠️ 期望码库中查无({len(missing)} 个,已从信号集剔除):{'、'.join(missing)}")
    if not signals or not noises:
        ap.error(f"信号({len(signals)})或噪声({len(noises)})为空,无法扫描")

    # scan grid
    grid = []
    t = args.lo
    while t <= args.hi + 1e-9:
        kept, sig_tot, cut, noi_tot = _rates(signals, noises, t)
        comb = (kept + cut) / (sig_tot + noi_tot)
        grid.append((round(t, 4), kept, sig_tot, cut, noi_tot, comb))
        t += args.step

    # max-keep floor: the highest floor that still keeps all real hits (= one step below the lowest signal sim)
    full_keep = [g for g in grid if g[1] == g[2]]
    max_keep = full_keep[-1] if full_keep else None
    # combined-accuracy best (reference only: sacrifices real courses to cut noise)
    best_comb = max(g[5] for g in grid)
    best = [g for g in grid if g[5] == best_comb][0]
    # current production value
    pk, pst, pc, pnt = _rates(signals, noises, PROD_MIN_SIM)

    sig_total = len(signals)
    noi_total = len(noises)
    print(f"\n=== 语义 floor 扫描(信号 {sig_total} 个真命中码 / 噪声 {noi_total} 门 off-topic 课)===")
    print(f"当前生产值 {PROD_MIN_SIM:.2f}:  真命中保留 {pk}/{pst} ({pk/pst*100:.0f}%) | "
          f"噪声裁剪 {pc}/{pnt} ({pc/pnt*100:.0f}%) | 综合 {(pk+pc)/(sig_total+noi_total)*100:.0f}%")
    if max_keep:
        print(f"最大保留 floor {max_keep[0]:.2f}:  真命中保留 {max_keep[1]}/{max_keep[2]} (100%) | "
              f"噪声裁剪 {max_keep[3]}/{max_keep[4]} ({max_keep[3]/max_keep[4]*100:.0f}%)"
              f"  <- 不流失真课的最高 floor")
    print(f"综合最优 floor {best[0]:.2f}(仅参考,会牺牲真课):真命中 {best[1]}/{best[2]} | "
          f"噪声裁剪 {best[3]}/{best[4]} | 综合 {best[5]*100:.0f}%")

    print("\nfloor | 真命中保留 | 噪声裁剪 | 综合")
    keypts = {PROD_MIN_SIM, max_keep[0] if max_keep else None, best[0]}
    for thr, kept, sig_tot, cut, noi_tot, comb in grid:
        if args.show or abs(thr * 100 % 5) < 1e-6 or thr in keypts:
            mark = ""
            if thr == PROD_MIN_SIM:
                mark = "  <- 生产"
            elif max_keep and thr == max_keep[0]:
                mark = "  <- 最大保留"
            elif thr == best[0]:
                mark = "  <- 综合最优"
            print(f"  {thr:.2f} | {kept:>2}/{sig_tot} ({kept/sig_tot*100:>3.0f}%) | "
                  f"{cut:>2}/{noi_tot} ({cut/noi_tot*100:>3.0f}%) | {comb*100:>3.0f}%{mark}")

    # separability: lowest signal sim vs highest noise sim; overlap = no single floor can split = irreducible error
    sig_sorted = sorted(signals, key=lambda s: s["sim"])
    noi_sorted = sorted(noises, key=lambda n: -n["sim"])
    sig_min = sig_sorted[0]
    noi_max = noi_sorted[0]
    print(f"\n信号最低 sim = {sig_min['sim']:.3f}({sig_min['topic']} {sig_min['code']})  <- floor 抬过它就丢真课")
    print(f"噪声最高 sim = {noi_max['sim']:.3f}({noi_max['topic']} {noi_max['code']})  <- floor 压不到它就漏噪声")
    if noi_max["sim"] >= sig_min["sim"]:
        overlap_noi = [n for n in noises if n["sim"] >= sig_min["sim"]]
        print(f"\n⚠️ 重叠区 [{sig_min['sim']:.3f}, {noi_max['sim']:.3f}]:任何单一 floor 都分不开 —— 不可约误差")
        print(f"   高于信号下限、压不掉的噪声({len(overlap_noi)} 门):")
        for n in sorted(overlap_noi, key=lambda n: -n["sim"]):
            print(f"     sim={n['sim']:.3f}  {n['topic']} {n['code']}")
        print("   -> 纯 floor 有天花板;真/空主题的甄别交 answer 相关性诚实指令(approach-3),不靠 floor。")
    else:
        print(f"\n✓ 信号/噪声完全可分:floor ∈ ({noi_max['sim']:.3f}, {sig_min['sim']:.3f}] 可 100% 分对。")

    # signal tail (real hits closest to the floor): the floor's constraint comes from these few
    print("\n信号尾部(最低 5 个,floor 的约束来源):")
    for s in sig_sorted[:5]:
        print(f"  sim={s['sim']:.3f}  {s['topic']} {s['code']}")


if __name__ == "__main__":
    main()
