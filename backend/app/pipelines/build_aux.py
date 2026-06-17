"""build_aux.py — load program-level extra rules: programs.aux_rules (all rules kept for reference) + program_exclude (banned-course table).
Read aux_rules.jsonl; programs.aux_rules is updated by program_id; program_exclude is delete-then-insert by program_id.
Records with no matching program are counted and reported, not silent.

Usage:
    python build_aux.py --in aux_rules.jsonl
"""
from __future__ import annotations
import os
import json
import argparse

import psycopg

from app.core.config import DSN

DDL = """
ALTER TABLE programs ADD COLUMN IF NOT EXISTS aux_rules JSONB;
CREATE TABLE IF NOT EXISTS program_exclude (
    program_id   TEXT,
    course_code  TEXT,
    PRIMARY KEY (program_id, course_code)
);
CREATE INDEX IF NOT EXISTS idx_pe_course ON program_exclude(course_code);
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="infile", default="data/aux_rules.jsonl")
    args = ap.parse_args()

    recs = [json.loads(l) for l in open(args.infile, encoding="utf-8") if l.strip()]

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
            np = nx = missing = 0
            for r in recs:
                pid = r["program_id"]
                aux = r.get("aux_rules") or []
                cur.execute("UPDATE programs SET aux_rules=%s WHERE program_id=%s",
                            (json.dumps(aux, ensure_ascii=False), pid))
                if cur.rowcount == 0:
                    missing += 1
                    print(f"  [warn] program {pid} 不在 programs 表,aux_rules 未挂载")
                    continue
                cur.execute("DELETE FROM program_exclude WHERE program_id=%s", (pid,))
                codes = sorted({c for rule in aux if rule.get("type") == "exclude"
                                for c in rule.get("exclude_codes", [])})
                for code in codes:
                    cur.execute("INSERT INTO program_exclude (program_id, course_code) VALUES (%s,%s) "
                                "ON CONFLICT DO NOTHING", (pid, code))
                np += 1
                nx += len(codes)
        conn.commit()
    print(f"入库 programs.aux_rules {np} 个 | program_exclude {nx} 行" +
          (f" | 跳过(无对应 program){missing}" if missing else ""))


if __name__ == "__main__":
    main()
