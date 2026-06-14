"""
backfill_plan_aux.py — 给「单顶层 plan-picker」程序回填 plan/group 级 level 约束(aux_rules)。

早先入库的 programs.rules 没抓 plan 容器 header 的 auxiliaryRules,例如 MEngSc Software 方向
(SOFTWX5528)的「Selected courses must include at least 8 units at level 7」、group D 的
「at most 4 units at level 4」。模拟器因此无法校验/展示这些 level 约束。本脚本只针对这类程序
(顶层单条规则 + 带子规则的 field/plan 选择器,如 5528 家族、2031 BSc Honours)用修好的
scraper 重抓,整体替换 rules JSONB(含新增 aux_rules)。其它程序不动。

幂等:重复运行结果一致。打印每个程序回填前后的 课数/plan数 与 level 约束数,数据漂移可见(规则 19)。

用法:
    python -m app.pipelines.backfill_plan_aux            # 自动识别受影响程序并回填
    python -m app.pipelines.backfill_plan_aux 5528 5530  # 仅指定 program_id
"""
from __future__ import annotations
import sys
import json

import psycopg

from app.core.config import DSN
from app.scrapers import program_scraper as ps


def _is_picker(rules) -> bool:
    """顶层只有一条规则、且它是带子规则 plan 的选择器(与 simulator._picker_rule 同口径)。"""
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
