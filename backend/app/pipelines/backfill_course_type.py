"""
backfill_course_type.py — backfill the derived course_type column for courses already loaded.

build_db computes course_type at load time; but the live DB was loaded earlier without this column (or it is all the default coursework).
This script idempotently adds the column + backfills each offering by classify_course_type, reusing the same decision logic (rule 15).
It does not reload or touch embedding, it only reads title/assessments and updates course_type.

Usage:
    python -m app.pipelines.backfill_course_type
"""
from __future__ import annotations
import collections

import psycopg

from app.core.config import DSN
from app.pipelines.build_db import classify_course_type

ADD_COL = ("ALTER TABLE courses ADD COLUMN IF NOT EXISTS "
           "course_type TEXT NOT NULL DEFAULT 'coursework'")


def main() -> None:
    tally: collections.Counter = collections.Counter()
    changed = 0
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(ADD_COL)
            rows = cur.execute(
                "SELECT offering_id, title, assessments, course_type FROM courses"
            ).fetchall()
            for offering_id, title, assessments, old in rows:
                ct = classify_course_type(title, assessments)
                tally[ct] += 1
                if ct != old:
                    cur.execute(
                        "UPDATE courses SET course_type=%s WHERE offering_id=%s",
                        (ct, offering_id),
                    )
                    changed += 1
        conn.commit()
    print(f"回填 {len(rows)} 个 offering;更新 {changed} 行;course_type 分布 {dict(tally)}")


if __name__ == "__main__":
    main()
