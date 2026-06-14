"""
backfill_course_type.py — 给已灌库的 courses 回填 course_type 派生列。

build_db 灌库时会算 course_type;但线上库是早先灌的、没有该列(或全是默认 coursework)。
本脚本幂等补列 + 按 classify_course_type 逐 offering 回填,复用同一套判定逻辑(规则 15)。
不重灌、不动 embedding,只读 title/assessments 改 course_type。

用法:
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
