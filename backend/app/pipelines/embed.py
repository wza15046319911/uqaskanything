"""
embed.py — stage three: compute embeddings for courses.search_blob and write the embedding column
Uses local Ollama bge-m3 (1024 dimensions). After it finishes, it builds an hnsw cosine index.

Usage:
    python embed.py          # only fill the ones not computed yet (embedding IS NULL)
    python embed.py --all    # recompute all (use after search_blob changed)
"""
from __future__ import annotations
import os
import time
import argparse

import requests
import psycopg

from app.core.config import DSN
OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL = "bge-m3"


def embed(text: str, retries: int = 4) -> list[float]:
    text = text[:8000]                       # avoid an over-long blob triggering a bge-m3 500 (the 8192 token limit)
    for i in range(retries):
        try:
            r = requests.post(f"{OLLAMA}/api/embeddings",
                              json={"model": MODEL, "prompt": text}, timeout=120)
            r.raise_for_status()
            return r.json()["embedding"]
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(2 * (i + 1))           # Ollama transient 500/timeout: back off and retry


def to_vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="全部重算(默认只补 embedding IS NULL)")
    args = ap.parse_args()

    where = "" if args.all else "WHERE embedding IS NULL"
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT offering_id, search_blob FROM courses {where}")
            rows = cur.fetchall()
            print(f"待算 {len(rows)} 行")

            done = 0
            skipped: list[str] = []
            failed: list[tuple[str, str]] = []
            for oid, blob in rows:
                if not blob:
                    skipped.append(oid)
                    continue
                try:
                    vec = to_vec(embed(blob))
                except Exception as e:
                    failed.append((oid, str(e)[:80]))      # still failing after retries: skip and record, do not stop the whole run
                    continue
                cur.execute("UPDATE courses SET embedding = %s::vector WHERE offering_id = %s",
                            (vec, oid))
                done += 1
                if done % 50 == 0:
                    conn.commit()                          # commit periodically, so a crash does not lose computed progress
                    print(f"  {done}/{len(rows)} (committed)")
            conn.commit()

            cur.execute("CREATE INDEX IF NOT EXISTS idx_courses_embedding "
                        "ON courses USING hnsw (embedding vector_cosine_ops)")
            conn.commit()

    print(f"完成:写入 {done} 个 embedding"
          + (f";空 blob 跳过 {len(skipped)}" if skipped else "")
          + (f";失败 {len(failed)}: {failed[:10]}" if failed else ""))


if __name__ == "__main__":
    main()
