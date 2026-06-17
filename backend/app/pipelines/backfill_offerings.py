"""
backfill_offerings.py — backfill the per-code "current semester offering" flags offered_s1 / offered_s2.

为什么不用 semester 文本列直接过滤:该列按 offering 存且不可靠——S1 行是 2026,S2 行多为去年 2025 的代理,
而真实的 2026 S2 开课清单在 S2_CODES(data/s2_course_codes.txt),不在列里。所以「本期是否开 S1/S2」
统一用按 code 派生的两个标记列(retrieval.build_where / both_semesters 都读它们)。

派生口径(确定性):
  offered_s2 = code ∈ S2_CODES(权威 2026 S2 清单)
  offered_s1 = code 在库里有 semester='S1' 的行(S1 已全量爬取,列即权威源,无单独文件)

幂等:每次按上述口径整列重算。会显式报告 S2_CODES 中「库里完全没有行」的码数(rule 19:不静默吞掉),
这些码只有课程码、无任何详情,任何方案都无法展示,本脚本也不为它们造空壳行。

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
