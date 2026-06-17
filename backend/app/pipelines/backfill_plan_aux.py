"""
backfill_plan_aux.py — backfill plan/group level constraints (aux_rules) for "single top-level plan-picker" programs.

Programs loaded earlier had programs.rules that did not capture the auxiliaryRules on the plan container header, for example the MEngSc Software field
(SOFTWX5528) "Selected courses must include at least 8 units at level 7", or group D's
"at most 4 units at level 4". The simulator therefore could not validate/show these level constraints. This script only targets such programs
(a single top-level rule + a field/plan picker that has sub-rules, such as the 5528 family and 2031 BSc Honours), re-scrapes with the fixed
scraper, and replaces the whole rules JSONB (including the new aux_rules). Other programs are left untouched.

Idempotent: repeated runs give the same result. It prints the course count/plan count and level-constraint count before and after backfill for each program, so data drift is visible (rule 19).

Usage:
    python -m app.pipelines.backfill_plan_aux            # auto-detect affected programs and backfill
    python -m app.pipelines.backfill_plan_aux 5528 5530  # only the given program_id
"""
from __future__ import annotations
import sys
import json

import psycopg

from app.core.config import DSN
from app.scrapers import program_scraper as ps


def _is_picker(rules) -> bool:
    """There is only one top-level rule and it is a picker over plans that have sub-rules (same definition as simulator._picker_rule)."""
    if not rules or len(rules) != 1:
        return False
    rule = rules[0]
    if rule.get("children_refs"):
        return False
    plans = [it for it in rule.get("items", []) if it.get("kind") == "plan"]
    return bool(plans) and any(p.get("rules") for p in plans)


def _count_aux(rules) -> int:
    n = 0
    for r in rules or []:
        for it in r.get("items", []):
            if it.get("kind") == "plan":
                n += len(it.get("aux_rules") or [])
                for sr in it.get("rules") or []:
                    n += len(sr.get("aux_rules") or [])
    return n


def main() -> None:
    ps.DELAY = 0.5
    targets = sys.argv[1:]
    failed: list[tuple[str, str]] = []
    with psycopg.connect(DSN) as conn:
        if not targets:
            rows = conn.execute("SELECT program_id, rules FROM programs").fetchall()
            targets = [pid for pid, rules in rows if _is_picker(rules)]
        print(f"待回填程序({len(targets)}): {targets}")
        ok = 0
        for pid in targets:
            old = conn.execute(
                "SELECT rules FROM programs WHERE program_id=%s", (pid,)).fetchone()
            old_rules = old[0] if old else []
            old_nc, old_npl = ps._count(old_rules or [])
            try:
                rec = ps.parse_program(pid, "2026", expand=True, max_depth=3)
            except Exception as e:
                failed.append((pid, f"{type(e).__name__}: {e}"))
                print(f"  [fail] {pid}: {type(e).__name__}: {e}")
                continue
            if not rec or not rec.get("rules"):
                failed.append((pid, "重抓无规则"))
                print(f"  [fail] {pid}: 重抓无规则,跳过(未改库)")
                continue
            nc, npl = ps._count(rec["rules"])
            naux = _count_aux(rec["rules"])
            drift = "" if (nc, npl) == (old_nc, old_npl) else f"  ⚠课/plan数变化 {old_nc}/{old_npl}->{nc}/{npl}"
            conn.execute("UPDATE programs SET rules=%s WHERE program_id=%s",
                         (json.dumps(rec["rules"], ensure_ascii=False), pid))
            ok += 1
            print(f"  [ok] {pid} {rec.get('title') or ''!r}: {nc}课/{npl}plan, level约束 {naux} 条{drift}")
        conn.commit()
    print(f"\n回填完成: 成功 {ok} | 失败 {len(failed)}")
    if failed:
        print(f"  失败明细: {failed}")


if __name__ == "__main__":
    main()
