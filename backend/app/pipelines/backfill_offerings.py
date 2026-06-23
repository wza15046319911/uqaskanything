"""
backfill_offerings.py — backfill the per-code "current semester offering" flags offered_s1 / offered_s2.

Why not filter on the semester text column directly: that column is stored per offering and is not reliable -- the S1 row is 2026, but the S2 row is mostly a proxy for last year's 2025,
while the real 2026 S2 offering list is in S2_CODES (data/s2_course_codes.txt), not in the column. So "is it offered this S1/S2"
goes through two flag columns derived per code (both retrieval.build_where / both_semesters read them).

Derivation (deterministic):
  offered_s2 = code in S2_CODES (the authoritative 2026 S2 list)
  offered_s1 = code has a row with semester='S1' in the DB (S1 is fully scraped, the column is the authoritative source, no separate file)

Idempotent: each run recomputes the whole column by the above rules. It explicitly reports the count of codes in S2_CODES that "have no row at all in the DB" (rule 19: do not swallow silently),
these codes only have a course code with no details, no plan can show them, and this script does not create empty shell rows for them either.

Usage:
    python -m app.pipelines.backfill_offerings
"""
from __future__ import annotations

import psycopg

from app.core.config import DSN, S2_CODES

ADD_COLS = (
    "ALTER TABLE courses ADD COLUMN IF NOT EXISTS offered_s1 BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE courses ADD COLUMN IF NOT EXISTS offered_s2 BOOLEAN NOT NULL DEFAULT FALSE",
    "CREATE INDEX IF NOT EXISTS idx_courses_offered_s1 ON courses(offered_s1)",
    "CREATE INDEX IF NOT EXISTS idx_courses_offered_s2 ON courses(offered_s2)",
)


def main() -> None:
    if not S2_CODES:
        raise RuntimeError("S2_CODES 为空(data/s2_course_codes.txt 缺失或空),拒绝整列清零 offered_s2")
    s2 = sorted(S2_CODES)
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            for stmt in ADD_COLS:
                cur.execute(stmt)
            cur.execute("UPDATE courses SET offered_s2 = (code = ANY(%s))", (s2,))
            cur.execute(
                "UPDATE courses SET offered_s1 = "
                "code IN (SELECT code FROM courses WHERE semester = 'S1')"
            )
            n_s1 = cur.execute(
                "SELECT count(DISTINCT code) FROM courses WHERE offered_s1").fetchone()[0]
            n_s2 = cur.execute(
                "SELECT count(DISTINCT code) FROM courses WHERE offered_s2").fetchone()[0]
            have_row = cur.execute(
                "SELECT count(DISTINCT code) FROM courses WHERE code = ANY(%s)", (s2,)).fetchone()[0]
            stale = cur.execute(
                "SELECT count(DISTINCT code) FROM courses "
                "WHERE semester = 'S2' AND NOT offered_s2").fetchone()[0]
        conn.commit()
    no_row = len(S2_CODES) - have_row
    print(f"offered_s1 置真 {n_s1} 门(库内有 S1 行)")
    print(f"offered_s2 置真 {n_s2} 门 / S2_CODES 共 {len(S2_CODES)} 门")
    print(f"S2_CODES 中库里无任何行(只有课程码、无详情、未造行) {no_row} 门")
    print(f"原 semester='S2' 但不在 2026 S2_CODES(去年开、今年不开,现已不计 S2) {stale} 门")


if __name__ == "__main__":
    main()
