"""
kb_eval.py — knowledge base M1.5 pilot recall check (acceptance point 2: can QA hit the right page)
(maps to plan.md sections 1.5 / 4; red line 7: evals must be quantified)

Use bge-m3 to compute embeddings for all chunks in chunks.jsonl (cached to chunk_vecs.jsonl),
run vector recall on a set of real student questions, print the top-k hits per question, and
automatically compute hit@1 / hit@3 against the expected page.

Note: this is a fast check that does not depend on Postgres. The real loading and retrieval go
through pgvector + app/services/retrieval.py (stage five), not the in-memory cosine here.

Usage (run from backend/, needs Ollama running with bge-m3 pulled):
    python -m app.pipelines.kb_eval
    python -m app.pipelines.kb_eval --k 3 --recompute
"""
from __future__ import annotations
import json
import math
import time
import argparse
from pathlib import Path

import requests

from app.core.config import DATA_DIR

OLLAMA = "http://localhost:11434"
EMBED_MODEL = "bge-m3"
RERANK_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"

# (real student question, url keyword of the expected hit page). Questions map to the 40 pages scraped in the pilot.
QUERIES = [
    ("How do I query a final grade I think is wrong?", "querying-result"),
    ("What is the late enrolment fee?", "late-enrolment-fees"),
    ("Where do I find UQ mobile apps?", "mobile-apps"),
    ("How can I get financial support or an emergency loan?", "financial-support"),
    ("What on-campus accommodation options does UQ offer?", "accommodation"),
    ("What software is free for UQ students?", "software-content"),
    ("How do I become a UQ student mentor?", "become-uq-mentor"),
    ("Cyber security awareness event details", "staying-safe-online"),
    ("Tropical Cyclone Alfred student updates", "tropical-cyclone-alfred"),
    ("Entry requirements for non school leavers admission", "non-school-leavers"),
    ("How do I reset my UQ password?", "a_id/2535"),
    ("I can't connect to the VPN, what should I do?", "a_id/2532"),
    ("How do I get a letter confirming my enrolment?", "a_id/64"),
    ("Do I still graduate if I do honours next year?", "a_id/33"),
    ("My wifi stopped working after I changed my password", "a_id/2512"),
]


def _embed(text: str, retries: int = 4) -> list[float]:
    for i in range(retries):
        try:
            r = requests.post(f"{OLLAMA}/api/embeddings",
                              json={"model": EMBED_MODEL, "prompt": text[:8000]},
                              timeout=120)
            r.raise_for_status()
            return r.json()["embedding"]
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(2 * (i + 1))


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def load_vectors(chunks: list[dict], cache: Path, recompute: bool) -> dict[str, list[float]]:
    """chunk id -> vector; cached to cache, not recomputed on a second run."""
    vecs: dict[str, list[float]] = {}
    if cache.exists() and not recompute:
        for ln in cache.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                o = json.loads(ln)
                vecs[o["id"]] = o["vec"]
    todo = [c for c in chunks if c["id"] not in vecs]
    if todo:
        print(f"算 embedding:{len(todo)} 个 chunk ...")
        with open(cache, "w" if recompute else "a", encoding="utf-8") as f:
            for i, c in enumerate(todo, 1):
                v = _embed(c["text"])
                vecs[c["id"]] = v
                f.write(json.dumps({"id": c["id"], "vec": v}) + "\n")
                if i % 20 == 0:
                    print(f"  {i}/{len(todo)}")
    return vecs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks", default=str(DATA_DIR / "kb" / "chunks.jsonl"))
    ap.add_argument("--cache", default=str(DATA_DIR / "kb" / "chunk_vecs.jsonl"))
    ap.add_argument("--k", type=int, default=3, help="top-k 召回")
    ap.add_argument("--recompute", action="store_true", help="忽略缓存重算 embedding")
    ap.add_argument("--rerank", action="store_true", help="bge-m3 召回后用 cross-encoder 重排")
    ap.add_argument("--rerank-model", default=RERANK_MODEL, help="cross-encoder 模型")
    ap.add_argument("--cand", type=int, default=30, help="重排候选池大小(top-N 进重排)")
    ap.add_argument("--golden", default=str(DATA_DIR / "kb" / "golden.jsonl"),
                    help="评测集 JSONL({q, expect});不存在则用内置 QUERIES")
    args = ap.parse_args()

    golden = Path(args.golden)
    if golden.exists():
        queries = [(o["q"], o["expect"]) for o in
                   (json.loads(l) for l in golden.read_text(encoding="utf-8").splitlines() if l.strip())]
    else:
        queries = QUERIES

    chunks_path = Path(args.chunks)
    if not chunks_path.exists():
        ap.error(f"找不到 {chunks_path};先跑 kb_parse")
    chunks = [json.loads(ln) for ln in chunks_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    print(f"chunks:{len(chunks)} | 问题:{len(queries)}")

    vecs = load_vectors(chunks, Path(args.cache), args.recompute)

    ce = None
    if args.rerank:
        from sentence_transformers import CrossEncoder
        print(f"加载 reranker:{args.rerank_model} ...")
        ce = CrossEncoder(args.rerank_model)

    base1 = base_k = re1 = re_k = 0
    for q, expect in queries:
        qv = _embed(q)
        ranked = sorted(chunks, key=lambda c: _cos(qv, vecs[c["id"]]), reverse=True)
        base_top = ranked[:args.k]
        b1 = expect in base_top[0]["url"]
        bk = any(expect in c["url"] for c in base_top)
        base1 += b1
        base_k += bk

        if ce is not None:
            cand = ranked[:args.cand]
            scores = ce.predict([(q, c["text"]) for c in cand])
            reranked = [c for _, c in sorted(zip(scores, cand), key=lambda x: x[0], reverse=True)]
            final_top = reranked[:args.k]
        else:
            final_top = base_top
        f1 = expect in final_top[0]["url"]
        fk = any(expect in c["url"] for c in final_top)
        re1 += f1
        re_k += fk

        mark = "✓1" if f1 else ("✓k" if fk else "✗ ")
        delta = "" if not ce else ("  [rerank 修正]" if f1 and not b1 else
                                   ("  [rerank 弄丢]" if b1 and not f1 else ""))
        print(f"\n[{mark}] Q: {q}  (预期含 '{expect}'){delta}")
        for c in final_top:
            hitm = "→" if expect in c["url"] else "  "
            print(f"   {hitm} {c['breadcrumb'][:60]}")
            print(f"        {c['url']}")

    n = len(queries)
    print(f"\n=== 召回评测(候选池 top-{args.cand})===")
    print(f"bge-m3 召回:      hit@1 = {base1}/{n} ({base1/n*100:.0f}%) | "
          f"hit@{args.k} = {base_k}/{n} ({base_k/n*100:.0f}%)")
    if ce is not None:
        print(f"+ cross-encoder 重排: hit@1 = {re1}/{n} ({re1/n*100:.0f}%) | "
              f"hit@{args.k} = {re_k}/{n} ({re_k/n*100:.0f}%)")


if __name__ == "__main__":
    main()
