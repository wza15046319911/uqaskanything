"""
build_programs.py — stage six load: programs + program_course
Read programs.jsonl -> programs (rules stored as JSONB, used by the simulator) + a flat program_course derived recursively.
Safe to re-run: programs upsert by primary key, program_course is delete-then-insert by program_id.

Usage:
    python build_programs.py --in programs.jsonl
"""
from __future__ import annotations
import os
import json
import argparse

import psycopg

from app.core.config import DSN

DDL = """
CREATE TABLE IF NOT EXISTS programs (
    program_id   TEXT PRIMARY KEY,
    title        TEXT,
    total_units  INTEGER,
    rules        JSONB
);
CREATE TABLE IF NOT EXISTS program_course (
    program_id        TEXT,
    course_code       TEXT,
    requirement_type  TEXT,
    course_list       TEXT,
    via_plan          TEXT DEFAULT '',
    plan_subtype      TEXT,
    equiv_group       TEXT DEFAULT ''
);
ALTER TABLE program_course ADD COLUMN IF NOT EXISTS equiv_group TEXT DEFAULT '';
ALTER TABLE programs ADD COLUMN IF NOT EXISTS rule_logic TEXT;
CREATE INDEX IF NOT EXISTS idx_pc_course  ON program_course(course_code);
CREATE INDEX IF NOT EXISTS idx_pc_program ON program_course(program_id);
"""


def _satisfiable_units(part: dict) -> float:
    """Satisfiable units of a part: the units of each standalone course + one choice per equivalence group (take the option with the largest units)."""
    tot = 0.0
    for it in part.get("items", []):
        if it.get("kind") == "course":
            tot += it.get("units") or 0
        elif it.get("kind") == "equivalence":
            opts = [o.get("units") or 0 for o in it.get("options", [])]
            tot += max(opts) if opts else 0
    return tot


def _is_mandatory_select(part: dict) -> bool:
    """A select-type part where units_min == satisfiable units (must take all, equiv can substitute) is treated as a mandatory core, not an elective.
    This excludes real electives that "pick a subset from a long list" (units_min < satisfiable units). It covers the Honours/research-type
    "Complete exactly N units" mandatory requirement (such as the research/thesis part of 2033)."""
    if part.get("select_type") == "all":
        return False
    need = part.get("units_min") or 0
    if need <= 0:
        return False
    if not any(it.get("kind") in ("course", "equivalence") for it in part.get("items", [])):
        return False
    return abs(need - _satisfiable_units(part)) < 1e-6


def flatten(rules: list, via_plan: str = "", plan_subtype: str = "") -> list[tuple]:
    """rule tree -> [(course_code, requirement_type, course_list, via_plan, plan_subtype, equiv_group)]

    equiv_group: a standalone course is ''; every member of an equivalence (choose-one) group shares the same group key
    (member codes sorted then joined with '|'), used later to fold several alternatives of the same slot into one slot.
    requirement_type: select_type='all' or a mandatory select part (see _is_mandatory_select) is core, the rest is elective.
    """
    out = []
    for r in rules:
        req = "core" if (r.get("select_type") == "all" or _is_mandatory_select(r)) else "elective"
        clist = r.get("title") or ""
        for it in r.get("items", []):
            k = it.get("kind")
            if k == "course" and it.get("code"):
                out.append((it["code"], req, clist, via_plan, plan_subtype, ""))
            elif k == "equivalence":
                codes = [o["code"] for o in it.get("options", []) if o.get("code")]
                gid = "|".join(sorted(codes))
                for code in codes:
                    out.append((code, req, clist, via_plan, plan_subtype, gid))
            elif k == "plan" and it.get("rules"):
                out += flatten(it["rules"], it.get("code") or "", it.get("subtype") or "")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="infile", default="data/programs.jsonl")
    args = ap.parse_args()

    progs = [json.loads(l) for l in open(args.infile, encoding="utf-8") if l.strip()]

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
            np = nc = 0
            for p in progs:
                pid = p["program_id"]
                cur.execute(
                    "INSERT INTO programs (program_id, title, total_units, rules, rule_logic) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (program_id) DO UPDATE SET "
                    "title=EXCLUDED.title, total_units=EXCLUDED.total_units, "
                    "rules=EXCLUDED.rules, rule_logic=EXCLUDED.rule_logic",
                    (pid, p.get("title"), p.get("total_units"),
                     json.dumps(p.get("rules") or [], ensure_ascii=False),
                     p.get("rule_logic")))
                np += 1

                cur.execute("DELETE FROM program_course WHERE program_id = %s", (pid,))
                seen = set()
                uniq = []
                for row in flatten(p.get("rules") or []):
                    if row not in seen:
                        seen.add(row)
                        uniq.append((pid,) + row)
                if uniq:
                    cur.executemany(
                        "INSERT INTO program_course "
                        "(program_id, course_code, requirement_type, course_list, via_plan, plan_subtype, equiv_group) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)", uniq)
                nc += len(uniq)
        conn.commit()
    print(f"灌入 programs {np} 个 | program_course {nc} 行")


if __name__ == "__main__":
    main()
