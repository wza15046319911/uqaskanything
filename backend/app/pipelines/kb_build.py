"""
kb_build.py — stage five: knowledge base chunk create tables + load (pgvector)
(matches section 5 of plan.md)

Read chunks_all.jsonl (article+faq) and chunk_vecs.jsonl (id->bge-m3 vectors, computed by kb_eval),
join them and write into the kb_chunks table. Vectors reuse the cache, not recomputed. Upsert by id, safe to re-run.
Chunks without a vector are skipped and reported (red line: not silently dropped).

Usage (run from backend/, needs :5433 pgvector + chunk_vecs.jsonl already computed):
    python -m app.pipelines.kb_build
    python -m app.pipelines.kb_build --chunks data/kb/chunks_all.jsonl
"""
from __future__ import annotations
import json
import argparse
from pathlib import Path

import psycopg

from app.core.config import DSN, DATA_DIR
EMBED_DIM = 1024  # bge-m3

DDL = f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS kb_chunks (
    id            TEXT PRIMARY KEY,
    url           TEXT,
    domain        TEXT,
    type          TEXT,
    source_type   TEXT,
    page_title    TEXT,
    breadcrumb    TEXT,
    h2            TEXT,
    h3            TEXT,
    text          TEXT,
    approx_tokens INTEGER,
    fetched_at    TEXT,
    lastmod       TEXT,
    embedding     VECTOR({EMBED_DIM})
);
CREATE INDEX IF NOT EXISTS idx_kb_chunks_type   ON kb_chunks(type);
CREATE INDEX IF NOT EXISTS idx_kb_chunks_domain ON kb_chunks(domain);
"""

COLS = ["id", "url", "domain", "type", "source_type", "page_title",
        "breadcrumb", "h2", "h3", "text", "approx_tokens", "fetched_at", "lastmod"]


def _to_vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def load_vecs(cache: Path) -> dict[str, list[float]]:
    vecs: dict[str, list[float]] = {}
    for ln in cache.read_text(encoding="utf-8").splitlines():
        if ln.strip():
            o = json.loads(ln)
            vecs[o["id"]] = o["vec"]
    return vecs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks", default=str(DATA_DIR / "kb" / "chunks_all.jsonl"))
    ap.add_argument("--vecs", default=str(DATA_DIR / "kb" / "chunk_vecs.jsonl"))
    args = ap.parse_args()

    chunks = [json.loads(l) for l in Path(args.chunks).read_text(encoding="utf-8").splitlines() if l.strip()]
    vecs = load_vecs(Path(args.vecs))
    print(f"chunks:{len(chunks)} | 向量:{len(vecs)}")

    placeholders = ",".join(["%s"] * len(COLS)) + ",%s::vector"
    updates = ",".join(f"{c}=EXCLUDED.{c}" for c in COLS if c != "id") + ",embedding=EXCLUDED.embedding"
    sql = (f"INSERT INTO kb_chunks ({','.join(COLS)},embedding) VALUES ({placeholders}) "
           f"ON CONFLICT (id) DO UPDATE SET {updates}")

    inserted = 0
    skipped: list[str] = []
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
            for c in chunks:
                vec = vecs.get(c["id"])
                if vec is None:
                    skipped.append(c["id"])
                    continue
                vals = [c.get(col) for col in COLS] + [_to_vec(vec)]
                cur.execute(sql, vals)
                inserted += 1
                if inserted % 500 == 0:
                    conn.commit()
                    print(f"  {inserted}/{len(chunks)}")
            conn.commit()
            cur.execute("CREATE INDEX IF NOT EXISTS idx_kb_chunks_embedding "
                        "ON kb_chunks USING hnsw (embedding vector_cosine_ops)")
            conn.commit()

    print(f"\n灌入 {inserted} 行 -> kb_chunks(DSN={DSN})")
    if skipped:
        print(f"  跳过 {len(skipped)} 个无向量 chunk(前 10):{skipped[:10]}")


if __name__ == "__main__":
    main()
