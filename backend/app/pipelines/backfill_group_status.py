"""
backfill_group_status.py — backfill the derived group_status column for courses already loaded.

build_db computes group_status at load time; but the live DB was loaded earlier without this column (or it is all the default unknown).
This script idempotently adds the column + backfills each offering by classify_group, reusing the same decision logic (rule 15).
It does not reload or touch embedding, it only reads assessments and updates group_status.

Usage:
    python -m app.pipelines.backfill_group_status
"""
from __future__ import annotations
import collections

import psycopg

from app.core.config import DSN
from app.pipelines.build_db import classify_group

ADD_COL = ("ALTER TABLE courses ADD COLUMN IF NOT EXISTS "
           "group_status TEXT NOT NULL DEFAULT 'unknown'")
ADD_IDX = "CREATE INDEX IF NOT EXISTS idx_courses_group ON courses(group_status)"


def main() -> None:
    tally: collections.Counter = collections.Counter()
    changed = 0
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(ADD_COL)
            cur.execute(ADD_IDX)
            rows = cur.execute(
                "SELECT offering_id, assessments, group_status FROM courses"
            ).fetchall()
            for offering_id, assessments, old in rows:
                gs = classify_group(assessments)
                tally[gs] += 1
                if gs != old:
                    cur.execute(
                        "UPDATE courses SET group_status=%s WHERE offering_id=%s",
                        (gs, offering_id),
                    )
                    changed += 1
        conn.commit()
    print(f"回填 {len(rows)} 个 offering;更新 {changed} 行;group_status 分布 {dict(tally)}")


if __name__ == "__main__":
    main()
