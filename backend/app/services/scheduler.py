"""
scheduler.py — 阶段三a:按学期排课(确定性状态无关函数,不调 LLM)

把一组已选课贪心拓扑装箱到 n 个学期:
  - 先修 DAG 决定先后(prereq_map;缺则不约束)
  - 每学期学分上限 units_cap(默认 8)
  - 开课学期 offering_map(缺则任意学期,标 verified_offering=False)
  - incompatible 软检查(同计划内互斥课只给 warning,不阻止放置)

数据现状:courses 仅有 S1 开课、无先修字段,故 offering_map / prereq_map 默认空,
排课先退化为「按学分装箱」,顺序与开课学期未核实(写进 warnings,不假装已核实)。
放不下的课进 unplaced(带原因);恒满足 placed + unplaced == 去重后的输入(零静默丢)。
"""
from __future__ import annotations
import re

from app.services.simulator import DEFAULT_UNITS


def _level(code: str) -> int:
    """课程级别 = 码里第一个数字(CSSE1001 -> 1),用于让低年级课优先排前。"""
    m = re.search(r"\d", code)
    return int(m.group()) if m else 9


def schedule(selected, prereq_map=None, offering_map=None, units_map=None,
             incompatible_map=None, units_cap=8.0, n_semesters=6,
             semester_labels=None, start_sem="S1"):
    selected = list(dict.fromkeys(c for c in selected if c))   # 去重保序
    prereq_map = prereq_map or {}
    offering_map = offering_map or {}
    units_map = units_map or {}
    incompatible_map = incompatible_map or {}
    # 入学学期决定 S1/S2 交替起点:S1 入学 -> 格 0=S1;S2 入学 -> 格 0=S2
    start_sem = "S2" if start_sem == "S2" else "S1"
    _other = "S2" if start_sem == "S1" else "S1"
    sem_kind = [start_sem if i % 2 == 0 else _other for i in range(n_semesters)]
    if semester_labels is None:
        semester_labels = [f"Y{i // 2 + 1} {sem_kind[i]}" for i in range(n_semesters)]

    sel_set = set(selected)
    warnings: list[str] = []
    unplaced: list[dict] = []

    def units_of(c):
        u = units_map.get(c)
        return float(u) if u is not None else DEFAULT_UNITS

    # incompatible 软检查(数据今天就有):同计划内互斥课只警告
    for c in selected:
        for other in incompatible_map.get(c, ()):  # other 可能是 set/list
            if other in sel_set and c < other:
                warnings.append(f"{c} 与 {other} 互斥(incompatible),不应同时计入同一计划")

    # 先修边只保留指向「计划内」课程的;计划外先修记 warning(不静默忽略)
    deps: dict[str, set] = {}
    for c in selected:
        reqs = set(prereq_map.get(c, ()))
        out_of_plan = reqs - sel_set
        if out_of_plan:
            warnings.append(
                f"{c} 的先修 {sorted(out_of_plan)} 不在所选课程内,排课忽略其顺序约束")
        deps[c] = reqs & sel_set

    # Kahn 拓扑排序;稳定 tie-break:(units 降序, code 升序)
    order: list[str] = []
    pending = {c: set(deps[c]) for c in selected}
    remaining = set(selected)
    while remaining:
        ready = [c for c in remaining if not pending[c]]
        if not ready:                                  # 有环:剩下的全进 unplaced
            for c in sorted(remaining):
                unplaced.append({"code": c, "reason": "prereq_cycle"})
            remaining.clear()
            break
        ready.sort(key=lambda c: (_level(c), -units_of(c), c))  # 低年级优先,再学分降序,再码升序
        nxt = ready[0]
        order.append(nxt)
        remaining.discard(nxt)
        for c in remaining:
            pending[c].discard(nxt)

    # 装箱
    sems = [{"label": semester_labels[i] if i < len(semester_labels) else f"Sem {i + 1}",
             "courses": [], "units": 0.0} for i in range(n_semesters)]
    sem_index: dict[str, int] = {}
    for c in order:
        u = units_of(c)
        min_sem = 0
        for p in deps[c]:
            if p in sem_index:
                min_sem = max(min_sem, sem_index[p] + 1)
        offered = offering_map.get(c)                  # set('S1'/'S2') 或 None=未知
        placed = False
        for s in range(min_sem, n_semesters):
            if offered is not None and sem_kind[s] not in offered:
                continue
            if sems[s]["units"] + u > units_cap:
                continue
            sems[s]["courses"].append(
                {"code": c, "units": u, "verified_offering": offered is not None})
            sems[s]["units"] += u
            sem_index[c] = s
            placed = True
            break
        if not placed:
            unplaced.append({"code": c, "reason": "no_fitting_semester"})

    unverified = sum(1 for s in sems for x in s["courses"] if not x["verified_offering"])
    if unverified:
        warnings.append(
            f"{unverified} 门课开课学期未知(courses 仅 S1 数据),已任意放置并标未核实")

    # 零静默丢:任何未进学期也未进 unplaced 的码,补记 unaccounted
    accounted = {x["code"] for s in sems for x in s["courses"]} | {u["code"] for u in unplaced}
    for c in sorted(sel_set - accounted):
        unplaced.append({"code": c, "reason": "unaccounted"})

    return {"semesters": sems, "unplaced": unplaced, "warnings": warnings}
