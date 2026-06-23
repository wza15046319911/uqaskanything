"""
guide_build.py — guide loading pipeline (create table + check gate + chunk + embedding + upsert)

Order of work: walk data/guides/*.md, run guide_check on each file first (if it fails, skip + count + report the reason, rule 19);
for the ones that pass, chunk by `##` section (reuse kb_parse.sections_from_markdown), embed each chunk with local Ollama bge-m3
(reuse embed.embed, the same 1024-dim vector space as courses / kb_chunks, Risk 5), and upsert into course_guides.
id = `{code}_{year}-{idx}`, safe to re-run; before reloading a file, delete the old chunks for that (course_code, year) first, to avoid leftovers.

Both the local DB and Supabase use this table: which DB to run against is decided by DATABASE_URL (default local :5433). Both sides use the same Ollama vectors,
matching the courses migration. **The output does not print a DSN that contains a password** (only host/db is shown), to avoid leaking it into the logs.

Usage (run from backend/, needs :5433 pgvector + Ollama bge-m3):
    python -m app.pipelines.guide_build
    python -m app.pipelines.guide_build --dir data/guides
    DATABASE_URL=<supabase-dsn> python -m app.pipelines.guide_build   # load the cloud DB
"""
from __future__ import annotations
import os
import glob
import argparse
from datetime import date
from urllib.parse import urlsplit

import psycopg

from app.services import retrieval
from app.pipelines import guide_check, kb_parse, embed
from app.core.config import DSN, DATA_DIR
EMBED_DIM = 1024  # bge-m3

DDL = f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS course_guides (
    id            TEXT PRIMARY KEY,
    course_code   TEXT NOT NULL,
    year          INTEGER NOT NULL,
    semester      TEXT,
    section       TEXT,
    text          TEXT NOT NULL,
    source        TEXT,
    profile_url   TEXT,
    checked_at    TEXT,
    embedding     VECTOR({EMBED_DIM})
);
CREATE INDEX IF NOT EXISTS idx_course_guides_code ON course_guides(course_code);
CREATE INDEX IF NOT EXISTS idx_course_guides_emb  ON course_guides USING hnsw (embedding vector_cosine_ops);
"""

COLS = ["id", "course_code", "year", "semester", "section", "text", "source",
        "profile_url", "checked_at"]


def _safe_dsn(dsn: str) -> str:
    """Mask the DSN: only echo host:port/dbname, never include the username or password (to stop the Supabase string leaking into the logs)."""
    try:
        u = urlsplit(dsn)
        return f"{u.hostname}:{u.port or ''}{u.path}"
    except Exception:
        return "(dsn hidden)"


def _sections(body: str) -> list[tuple[str, str]]:
    """body -> [(section_title, chunk_text)]; chunk by ##, or the whole text as one chunk if there is no section. The embedding input carries the section title as a topic anchor."""
    secs = kb_parse.sections_from_markdown(body)
    out: list[tuple[str, str]] = []
    for h2, h3, text in secs:
        title = " ".join(t for t in (h2, h3) if t).strip()
        out.append((title, text.strip()))
    if not out and body.strip():
        out.append(("", body.strip()))
    return out


def build(conn, paths: list[str]) -> tuple[int, list[tuple[str, str]], dict]:
    """Chunk and load the guides that pass the check; return (number of chunks loaded, [(path, skip reason)], {code: checked_at})."""
    placeholders = ",".join(["%s"] * len(COLS)) + ",%s::vector"
    updates = ",".join(f"{c}=EXCLUDED.{c}" for c in COLS if c != "id") + ",embedding=EXCLUDED.embedding"
    sql = (f"INSERT INTO course_guides ({','.join(COLS)},embedding) VALUES ({placeholders}) "
           f"ON CONFLICT (id) DO UPDATE SET {updates}")

    today = date.today().isoformat()
    inserted = 0
    skipped: list[tuple[str, str]] = []
    checked: dict[str, str] = {}
    with conn.cursor() as cur:
        cur.execute(DDL)
        conn.commit()
        for path in paths:
            res = guide_check.check_file(conn, path)
            if not res["ok"]:
                skipped.append((path, "; ".join(res["conflicts"])))
                continue
            fm, body = res["frontmatter"], res["body"]
            code = res["code"]
            year = int(fm.get("year"))
            semester = str(fm.get("semester") or "") or None
            source = str(fm.get("source") or "") or None
            profile_url = retrieval.COURSE_PROFILE_URL.format(code)
            secs = _sections(body)
            # before reloading a file, clear the old chunks for this (code, year) first, to avoid stale leftover chunks after the text is shortened
            cur.execute("DELETE FROM course_guides WHERE course_code=%s AND year=%s", (code, year))
            for idx, (section, text) in enumerate(secs):
                vec = embed.to_vec(embed.embed(f"{section}\n{text}".strip()))
                vals = [f"{code}_{year}-{idx}", code, year, semester, section or None,
                        text, source, profile_url, today, vec]
                cur.execute(sql, vals)
                inserted += 1
            checked[code] = today
            conn.commit()
        cur.execute("CREATE INDEX IF NOT EXISTS idx_course_guides_emb "
                    "ON course_guides USING hnsw (embedding vector_cosine_ops)")
        conn.commit()
    return inserted, skipped, checked


def _expand(directory: str) -> list[str]:
    """All guide files in the directory that match <CODE>_<YEAR>.md (filtering out README and the like)."""
    return [p for p in sorted(glob.glob(os.path.join(directory, "*.md")))
            if guide_check._GUIDE_FILE_RE.match(os.path.basename(p))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(DATA_DIR / "guides"), help="攻略目录(默认 data/guides)")
    args = ap.parse_args()
    paths = _expand(args.dir)
    if not paths:
        print(f"{args.dir} 下没有 <CODE>_<YEAR>.md 攻略文件")
        return 1
    print(f"待处理 {len(paths)} 篇 -> course_guides({_safe_dsn(DSN)})")
    with psycopg.connect(DSN) as conn:
        inserted, skipped, checked = build(conn, paths)
    print(f"\n入库 {inserted} 块;对账通过 {len(checked)} 篇" + (f",跳过 {len(skipped)} 篇" if skipped else ""))
    for code, d in sorted(checked.items()):
        print(f"  ✓ {code}  checked_at={d}")
    for path, why in skipped:
        print(f"  ✗ SKIP {path}:{why}")
    return 1 if skipped else 0


if __name__ == "__main__":
    raise SystemExit(main())
