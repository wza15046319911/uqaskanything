"""
scheduler.py — stage 3a: place courses into semesters (deterministic state-free function, no LLM)

Greedy topological bin-packing of a set of selected courses into n semesters:
  - prerequisite DAG decides order (prereq_map; no constraint if missing)
  - per-semester units cap units_cap (default 8)
  - offering semester offering_map (any semester if missing, mark verified_offering=False)
  - year-long courses year_long lock to S1 start, take two consecutive semesters [s, s+1], units split half each (units/2)
  - incompatible soft check (mutually exclusive courses in the same plan only give a warning, do not block placement)

Current data: courses only has S1 offering and no prerequisite field, so offering_map / prereq_map default to empty,
scheduling first degrades to "pack by units", order and offering semester are not verified (written into warnings, not pretending to be verified).
Courses that do not fit go into unplaced (with reason); always holds placed + unplaced == deduplicated input (zero silent drop).
"""
from __future__ import annotations
import re

from app.services.simulator import DEFAULT_UNITS


def _level(code: str) -> int:
    """Course level = first digit in the code (CSSE1001 -> 1), used to place lower-year courses earlier."""
    m = re.search(r"\d", code)
    return int(m.group()) if m else 9


def schedule(selected, prereq_map=None, offering_map=None, units_map=None,
             incompatible_map=None, units_cap=8.0, n_semesters=6,
             semester_labels=None, start_sem="S1", year_long=None):
    selected = list(dict.fromkeys(c for c in selected if c))   # dedup, keep order
    prereq_map = prereq_map or {}
    offering_map = offering_map or {}
    units_map = units_map or {}
    incompatible_map = incompatible_map or {}
    year_long = set(year_long or ())
    # entry semester decides the S1/S2 alternation start: S1 entry -> slot 0=S1; S2 entry -> slot 0=S2
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

    # incompatible soft check (data exists today): mutually exclusive courses in the same plan only warn
    for c in selected:
        for other in incompatible_map.get(c, ()):  # other may be set/list
            if other in sel_set and c < other:
                warnings.append(f"{c} 与 {other} 互斥(incompatible),不应同时计入同一计划")

    # only keep prerequisite edges pointing to "in-plan" courses; out-of-plan prerequisites get a warning (not silently ignored)
    deps: dict[str, set] = {}
    for c in selected:
        reqs = set(prereq_map.get(c, ()))
        out_of_plan = reqs - sel_set
        if out_of_plan:
            warnings.append(
                f"{c} 的先修 {sorted(out_of_plan)} 不在所选课程内,排课忽略其顺序约束")
        deps[c] = reqs & sel_set

    # Kahn topological sort; stable tie-break: (units desc, code asc)
    order: list[str] = []
    pending = {c: set(deps[c]) for c in selected}
    remaining = set(selected)
    while remaining:
        ready = [c for c in remaining if not pending[c]]
        if not ready:                                  # cycle: all remaining go to unplaced
            for c in sorted(remaining):
                unplaced.append({"code": c, "reason": "prereq_cycle"})
            remaining.clear()
            break
        ready.sort(key=lambda c: (_level(c), -units_of(c), c))  # lower year first, then units desc, then code asc
        nxt = ready[0]
        order.append(nxt)
        remaining.discard(nxt)
        for c in remaining:
            pending[c].discard(nxt)

    # bin-packing
    sems = [{"label": semester_labels[i] if i < len(semester_labels) else f"Sem {i + 1}",
             "courses": [], "units": 0.0} for i in range(n_semesters)]
    sem_index: dict[str, int] = {}

    def end_sem(p):                                    # year-long course spans two slots, finishes in the continuation slot
        return sem_index[p] + (1 if p in year_long else 0)

    for c in order:
        u = units_of(c)
        yl = c in year_long
        per = u / 2 if yl else u                        # year-long units split across two consecutive semesters
        min_sem = 0
        for p in deps[c]:
            if p in sem_index:
                min_sem = max(min_sem, end_sem(p) + 1)
        offered = offering_map.get(c)                  # set('S1'/'S2') or None=unknown
        placed = False
        for s in range(min_sem, n_semesters):
            if offered is not None and sem_kind[s] not in offered:
                continue
            if yl:                                     # year-long: lock to S1 start, take [s, s+1] half each
                if sem_kind[s] != "S1" or s + 1 >= n_semesters:
                    continue
                if (sems[s]["units"] + per > units_cap
                        or sems[s + 1]["units"] + per > units_cap):
                    continue
                for j, part in ((s, "start"), (s + 1, "continuation")):
                    sems[j]["courses"].append(
                        {"code": c, "units": per, "full_units": u,
                         "verified_offering": offered is not None,
                         "year_long": True, "part": part})
                    sems[j]["units"] += per
                sem_index[c] = s
                placed = True
                break
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

    unverified = sum(1 for s in sems for x in s["courses"]
                     if not x["verified_offering"] and x.get("part") != "continuation")
    if unverified:
        warnings.append(
            f"{unverified} 门课开课学期未知(courses 仅 S1 数据),已任意放置并标未核实")

    # zero silent drop: any code not placed in a semester and not in unplaced, record as unaccounted
    accounted = {x["code"] for s in sems for x in s["courses"]} | {u["code"] for u in unplaced}
    for c in sorted(sel_set - accounted):
        unplaced.append({"code": c, "reason": "unaccounted"})

    return {"semesters": sems, "unplaced": unplaced, "warnings": warnings}
