"""
build_programs.py — 阶段六入库:programs + program_course
读 programs.jsonl -> programs(rules 存 JSONB,供模拟器)+ 递归派生扁平 program_course。
可重复运行:programs 按主键 upsert,program_course 按 program_id 先删后插。

用法:
    python build_programs.py --in programs.jsonl
"""
from __future__ import annotations
import os
import json
import argparse

import psycopg

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres:uqrag@localhost:5433/uq_courses")

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
    """部分的可满足单元:每门 standalone course 的 units + 每个 equivalence 组选一(取选项最大 units)。"""
    tot = 0.0
    for it in part.get("items", []):
        if it.get("kind") == "course":
            tot += it.get("units") or 0
        elif it.get("kind") == "equivalence":
            opts = [o.get("units") or 0 for o in it.get("options", [])]
            tot += max(opts) if opts else 0
    return tot


def _is_mandatory_select(part: dict) -> bool:
    """select 型部分若 units_min==可满足单元(必须全修、equiv 可替代),视为强制核心而非选修。
    排除"从长列表里选子集"的真选修(units_min < 可满足单元)。覆盖 Honours/研究类的
    "Complete exactly N units" 强制要求(如 2033 的 research/thesis 部分)。"""
    if part.get("select_type") == "all":
        return False
    need = part.get("units_min") or 0
    if need <= 0:
        return False
    if not any(it.get("kind") in ("course", "equivalence") for it in part.get("items", [])):
        return False
    return abs(need - _satisfiable_units(part)) < 1e-6


def flatten(rules: list, via_plan: str = "", plan_subtype: str = "") -> list[tuple]:
    """规则树 -> [(course_code, requirement_type, course_list, via_plan, plan_subtype, equiv_group)]

    equiv_group:standalone 课为 ''；equivalence(二选一)组的每个成员共享同一组键
    (成员码排序后 '|' 连接),用于后续把同一槽位的多个备选折叠成 1 个槽位。
    requirement_type:select_type='all' 或强制 select 部分(见 _is_mandatory_select)为 core,其余 elective。
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
    ap.add_argument("--in", dest="infile", default="programs.jsonl")
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
