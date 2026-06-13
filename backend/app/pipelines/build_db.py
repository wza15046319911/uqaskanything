"""
build_db.py — 阶段三:建表 + 灌库
读 courses.jsonl 写入 Postgres(pgvector)。embedding 列留空,由 embed.py 填充。
可重复运行:按 offering_id upsert,不会重复插入,也不会覆盖已算好的 embedding。

用法:
    python build_db.py --in courses.jsonl
"""
from __future__ import annotations
import os
import re
import json
import argparse
from datetime import date

import psycopg

from app.core.config import DSN
EMBED_DIM = 1024  # bge-m3
YEAR_LONG_MIN_DAYS = 240  # 授课跨度阈值:标准学期约 120 天、最长短学期约 165 天、年课约 270 天

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

-- 年课标记:派生自 study_period 授课跨度(跨连续两学期),供排课器平摊学分
ALTER TABLE courses ADD COLUMN IF NOT EXISTS is_year_long BOOLEAN;
"""

COLS = ["offering_id", "code", "title", "study_period", "semester", "year",
        "location", "attendance_mode", "level", "units", "coordinating_unit",
        "coordinator", "has_exam", "has_hurdle", "incompatible", "assessments",
        "learning_outcomes", "topics", "learning_activities", "description",
        "search_blob", "prerequisite_raw", "prerequisite_parsed", "is_year_long"]
JSON_COLS = {"incompatible", "assessments", "learning_outcomes", "topics",
             "learning_activities"}


def is_year_long(study_period: str | None) -> bool | None:
    """据 study_period 授课起止跨度判断是否年课(横跨连续两学期)。

    形如 "Semester 1, 2026 (23/02/2026 - 21/11/2026)":跨度 >= YEAR_LONG_MIN_DAYS
    即年课。无法解析出日期区间返回 None(交调用方显式计数,不静默当成非年课)。
    """
    if not study_period:
        return None
    m = re.search(r"\((\d{2})/(\d{2})/(\d{4})\s*-\s*(\d{2})/(\d{2})/(\d{4})\)", study_period)
    if not m:
        return None
    d1, mo1, y1, d2, mo2, y2 = (int(x) for x in m.groups())
    try:
        span = (date(y2, mo2, d2) - date(y1, mo1, d1)).days
    except ValueError:
        return None
    return span >= YEAR_LONG_MIN_DAYS


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
            n = n_yl = n_unparsed = 0
            for c in rows:
                yl = is_year_long(c.get("study_period"))
                c["is_year_long"] = yl
                if yl is None:
                    n_unparsed += 1
                elif yl:
                    n_yl += 1
                cur.execute(sql, row_values(c))
                n += 1
        conn.commit()
    print(f"灌入 {n} 行 -> courses(DSN={DSN});年课 {n_yl} 门;"
          f"study_period 无法解析 {n_unparsed} 门(is_year_long=NULL)")


if __name__ == "__main__":
    main()
