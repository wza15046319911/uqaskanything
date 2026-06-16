"""
backfill_group_status.py — 给已灌库的 courses 回填 group_status 派生列。

build_db 灌库时会算 group_status;但线上库是早先灌的、没有该列(或全是默认 unknown)。
本脚本幂等补列 + 按 classify_group 逐 offering 回填,复用同一套判定逻辑(规则 15)。
不重灌、不动 embedding,只读 assessments 改 group_status。

用法:
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
