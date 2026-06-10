"""
build_db.py — 阶段三:建表 + 灌库
读 courses.jsonl 写入 Postgres(pgvector)。embedding 列留空,由 embed.py 填充。
可重复运行:按 offering_id upsert,不会重复插入,也不会覆盖已算好的 embedding。

用法:
    python build_db.py --in courses.jsonl
"""
from __future__ import annotations
import os
import json
import argparse

import psycopg

from app.core.config import DSN
EMBED_DIM = 1024  # bge-m3

DDL = f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS courses (
    offering_id         TEXT PRIMARY KEY,
    code                TEXT,
    title               TEXT,
    study_period        TEXT,
    semester            TEXT,
    year                INTEGER,
    location            TEXT,
    attendance_mode     TEXT,
    level               TEXT,
    units               REAL,
    coordinating_unit   TEXT,
    coordinator         TEXT,
    has_exam            BOOLEAN,
    has_hurdle          BOOLEAN,
    incompatible        JSONB,
    assessments         JSONB,
    learning_outcomes   JSONB,
    topics              JSONB,
    learning_activities JSONB,
    description         TEXT,
    search_blob         TEXT,
    embedding           VECTOR({EMBED_DIM})
);
CREATE INDEX IF NOT EXISTS idx_courses_code     ON courses(code);
CREATE INDEX IF NOT EXISTS idx_courses_semester ON courses(semester);
CREATE INDEX IF NOT EXISTS idx_courses_year     ON courses(year);
CREATE INDEX IF NOT EXISTS idx_courses_level    ON courses(level);
CREATE INDEX IF NOT EXISTS idx_courses_has_exam ON courses(has_exam);

-- 阶段三b 先修(已建库则幂等补列)
ALTER TABLE courses ADD COLUMN IF NOT EXISTS prerequisite_raw    TEXT;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS prerequisite_parsed JSONB;
"""

COLS = ["offering_id", "code", "title", "study_period", "semester", "year",
        "location", "attendance_mode", "level", "units", "coordinating_unit",
        "coordinator", "has_exam", "has_hurdle", "incompatible", "assessments",
        "learning_outcomes", "topics", "learning_activities", "description",
        "search_blob", "prerequisite_raw", "prerequisite_parsed"]
JSON_COLS = {"incompatible", "assessments", "learning_outcomes", "topics",
             "learning_activities"}


def row_values(c: dict) -> list:
    vals = []
    for col in COLS:
        v = c.get(col)
        if col == "prerequisite_parsed":
            # None 必须存 JSON null(无先修),不能退化成 []——要与「未爬到」区分
            v = json.dumps(v, ensure_ascii=False)
        elif col in JSON_COLS:
            v = json.dumps(v if v is not None else [], ensure_ascii=False)
        vals.append(v)
    return vals


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="infile", default="data/courses.jsonl")
    args = ap.parse_args()

    rows = [json.loads(l) for l in open(args.infile, encoding="utf-8") if l.strip()]
    placeholders = ",".join(["%s"] * len(COLS))
    updates = ",".join(f"{c}=EXCLUDED.{c}" for c in COLS if c != "offering_id")
    sql = (f"INSERT INTO courses ({','.join(COLS)}) VALUES ({placeholders}) "
           f"ON CONFLICT (offering_id) DO UPDATE SET {updates}")

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
            n = 0
            for c in rows:
                cur.execute(sql, row_values(c))
                n += 1
        conn.commit()
    print(f"灌入 {n} 行 -> courses(DSN={DSN})")


if __name__ == "__main__":
    main()
