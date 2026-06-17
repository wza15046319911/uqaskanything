"""Course planner simulator API (/api/sim/*).

Deterministic, no LLM in state computation; the client holds the state and the
server replays it statelessly.
(/api/sim/advise is the exception: deterministic candidate pool + LLM only ranks
and explains + dual guardrails.)
"""
from __future__ import annotations

import psycopg
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core.config import DSN, S2_CODES
from app.services import simulator, scheduler, sim_advise

router = APIRouter(prefix="/api/sim")


class SimState(BaseModel):
    program_id: str = "2559"
    selected: list[str] = []
    chosen_plans: list[str] = []
    branch: list[str] = []             # chosen branch ref for an OR group (e.g. ["C"]=No-Major; default is the first of each group)
    placement: dict[str, int] = {}     # code -> semester slot index (0=Y1S1, 1=Y1S2, ...); used by the timetable
    units_cap: float = 8.0
    n_semesters: int = 6
    start_sem: str = "S1"              # entry semester: "S1" or "S2" (decides whether slot 0 is S1 or S2)


def _offerings(conn, codes) -> dict:
    """code -> list of offering semesters (S1 from courses.semester, S2 from the 2026:2 search page list)."""
    codes = list(codes)
    off: dict[str, set] = {}
    if codes:
        for code, sem in conn.execute(
            "SELECT DISTINCT code, semester FROM courses WHERE code = ANY(%s)", (codes,)
        ).fetchall():
            if sem:
                off.setdefault(code, set()).add(sem)
    for c in codes:
        if c in S2_CODES:
            off.setdefault(c, set()).add("S2")
    return {c: sorted(s) for c, s in off.items()}


def _validate(sim, placement, offerings, inc_map, n_sem, cap, start_sem="S1",
              year_long=None) -> dict:
    """Timetable placement check (deterministic): offering semester / prereqs in earlier semesters / units cap / incompatibility / year-long courses spanning two semesters."""
    um = sim.units_map()
    year_long = set(year_long or ())
    start_sem = "S2" if start_sem == "S2" else "S1"
    _other = "S2" if start_sem == "S1" else "S1"
    kind = lambda i: start_sem if i % 2 == 0 else _other
    sem_units = [0.0] * n_sem
    placed = {c: i for c, i in placement.items() if isinstance(i, int) and 0 <= i < n_sem}
    by_course: dict[str, list] = {}
    for c, i in placed.items():
        # out-of-tree courses (from E/F search) look up units in the whole DB, in-tree ones use the rule tree; default only when both are missing
        u = float(um.get(c) or sim._course_units.get(c) or simulator.DEFAULT_UNITS)
        yl = c in year_long
        per = u / 2 if yl else u                        # year-long course units split evenly across two consecutive semesters
        reasons = []
        sem_units[i] += per
        if yl:                                          # year-long course occupies [i, i+1]; error if the last slot has no following slot
            if i + 1 < n_sem:
                sem_units[i + 1] += per
            else:
                reasons.append({"type": "year_long", "msg": "年课需占后续学期,当前已是最后一格"})
        off = offerings.get(c)
        if yl and not off:                              # year-long course is locked to start in S1 (locked even without offering data)
            off = ["S1"]
        if off and kind(i) not in off:
            reasons.append({"type": "offering", "msg": f"{kind(i)} 不开课(开课:{'/'.join(off)})"})
        tree = sim._prereq.get(c)
        if tree:
            earlier = {x for x, j in placed.items() if j < i}
            ok, why = simulator.satisfied(tree, earlier)
            if not ok:
                reasons.append({"type": "prereq", "msg": f"先修未在更早学期:{why}"})
        for other in inc_map.get(c, ()):
            if other in placed:
                reasons.append({"type": "incompatible", "msg": f"与 {other} 互斥"})
        if reasons:
            by_course[c] = reasons
    cap_over = [i for i, u in enumerate(sem_units) if u > cap]
    return {"by_course": by_course, "semester_units": sem_units, "cap_over": cap_over, "cap": cap}


def _slot_codes(by_rule: dict) -> set[str]:
    codes: set[str] = set()
    for slots in by_rule.values():
        for s in slots:
            if s["kind"] == "course":
                codes.add(s["code"])
            else:
                codes.update(s["options"])
    return codes


def _hydrate(conn, codes) -> dict:
    codes = list(codes)
    if not codes:
        return {}
    rows = conn.execute(
        "SELECT DISTINCT ON (code) code, title, units, level, semester, has_exam "
        "FROM courses WHERE code = ANY(%s) ORDER BY code",
        (codes,),
    ).fetchall()
    return {
        r[0]: {"code": r[0], "title": r[1], "units": r[2],
               "level": r[3], "semester": r[4], "has_exam": r[5]}
        for r in rows
    }


@router.get("/programs")
def sim_programs():
    with psycopg.connect(DSN) as conn:
        conn.read_only = True
        rows = conn.execute(
            "SELECT program_id, title, total_units FROM programs ORDER BY title"
        ).fetchall()
    return [{"program_id": r[0], "title": r[1], "total_units": r[2]} for r in rows]


@router.get("/program/{program_id}")
def sim_program(program_id: str):
    try:
        with psycopg.connect(DSN) as conn:
            conn.read_only = True
            sim = simulator.PlanSimulator(conn, program_id)
            return {"program_id": program_id, "title": sim.title,
                    "total_units": sim.total_units, "rules": sim.status()}
    except ValueError as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)


@router.post("/state")
def sim_state(body: SimState):
    try:
        with psycopg.connect(DSN) as conn:
            conn.read_only = True
            try:
                sim = simulator.PlanSimulator(conn, body.program_id)
            except ValueError as e:
                return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=404)
            try:
                for c in body.selected:
                    sim.select(c)
                for p in body.chosen_plans:
                    sim.choose_plan(p)          # unknown / self-referencing plan code raises ValueError
                for ref in body.branch:
                    sim.choose_branch(ref)      # unknown branch ref raises ValueError
            except ValueError as e:
                return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=400)
            by_rule = sim.available_by_rule()
            codes = _slot_codes(by_rule) | set(body.selected)
            locks = {d["code"]: d for d in sim.available_detailed()
                     if d["state"] != "unlocked"}   # only expose locked/unknown
            offerings = _offerings(conn, codes)
            inc_map: dict[str, set] = {}
            placed = [c for c in body.placement if isinstance(body.placement[c], int)]
            year_long: set[str] = set()
            if placed:
                for code, inc, yl in conn.execute(
                    "SELECT DISTINCT code, incompatible, is_year_long FROM courses "
                    "WHERE code = ANY(%s)",
                    (placed,),
                ).fetchall():
                    if inc:
                        inc_map.setdefault(code, set()).update(inc)
                    if yl:
                        year_long.add(code)
            validation = _validate(sim, body.placement, offerings, inc_map,
                                   body.n_semesters, body.units_cap, body.start_sem,
                                   year_long)
            return {
                "program_id": body.program_id,
                "title": sim.title,
                "total_units": sim.total_units,
                "selected": body.selected,
                "chosen_plans": body.chosen_plans,
                "rules": sim.status(),
                "available_by_rule": by_rule,
                "selected_by_rule": sim.selected_by_rule(),
                "overall": sim.overall(),
                "locks": locks,
                "offerings": offerings,
                "validation": validation,
                "level_caps": sim.level_cap_status(),
                "courses": _hydrate(conn, codes),
            }
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)


@router.get("/courses")
def sim_courses(q: str = "", in_program: str = ""):
    """Course search (for E/F section selection): code/title ILIKE; when in_program=<pid>, limit to codes within that program's course list."""
    q = q.strip()
    if len(q) < 2:
        return JSONResponse({"error": "搜索词至少 2 个字符"}, status_code=400)
    try:
        with psycopg.connect(DSN) as conn:
            conn.read_only = True
            sql = ("SELECT DISTINCT ON (code) code, title, units, level, semester, has_exam "
                   "FROM courses WHERE (code ILIKE %s OR title ILIKE %s)")
            params: list = [f"%{q}%", f"%{q}%"]
            if in_program:
                try:
                    sim = simulator.PlanSimulator(conn, in_program)
                except ValueError as e:
                    return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=404)
                sql += " AND code = ANY(%s)"
                params.append(sorted(sim._all_referenced_codes()))
            sql += " ORDER BY code LIMIT 50"
            rows = conn.execute(sql, params).fetchall()
            offerings = _offerings(conn, [r[0] for r in rows])
            return [{"code": r[0], "title": r[1], "units": r[2], "level": r[3],
                     "semester": r[4], "has_exam": r[5],
                     "offerings": offerings.get(r[0], [])} for r in rows]
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)


class SimSchedule(BaseModel):
    program_id: str = "2559"
    selected: list[str] = []
    chosen_plans: list[str] = []
    units_cap: float = 8.0
    start_sem: str = "S1"


def _prereq_codes(tree) -> set[str]:
    out: set[str] = set()
    stack = [tree] if tree else []
    while stack:
        n = stack.pop()
        if n.get("op") == "course":
            out.add(n["code"])
        else:
            stack += n.get("children", [])
    return out


@router.post("/schedule")
def sim_schedule(body: SimSchedule):
    try:
        with psycopg.connect(DSN) as conn:
            conn.read_only = True
            try:
                sim = simulator.PlanSimulator(conn, body.program_id)
            except ValueError as e:
                return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=404)
            for c in body.selected:
                sim.select(c)
            inc_map: dict[str, set] = {}
            offering_map: dict[str, set] = {}
            year_long: set[str] = set()
            if body.selected:
                for code, inc, sem, yl in conn.execute(
                    "SELECT DISTINCT code, incompatible, semester, is_year_long FROM courses "
                    "WHERE code = ANY(%s)", (body.selected,),
                ).fetchall():
                    if inc:
                        inc_map.setdefault(code, set()).update(inc)
                    if sem:                                  # offering semester from the courses table (S1)
                        offering_map.setdefault(code, set()).add(sem)
                    if yl:                                   # year-long course: spans two consecutive semesters
                        year_long.add(code)
                for c in body.selected:                      # S2 offering: runs in S2 if it appears in the S2 list
                    if c in S2_CODES:
                        offering_map.setdefault(c, set()).add("S2")
            prereq_map = {c: _prereq_codes(sim._prereq.get(c))
                          for c in body.selected if sim._prereq.get(c)}
            units = {c: sim._course_units.get(c, simulator.DEFAULT_UNITS)
                     for c in body.selected}       # out-of-tree courses (E/F) fall back to whole-DB units
            units.update(sim.units_map())
            result = scheduler.schedule(
                body.selected, prereq_map=prereq_map, units_map=units,
                incompatible_map=inc_map, offering_map=offering_map or None,
                units_cap=body.units_cap, start_sem=body.start_sem,
                year_long=year_long)
            result["courses"] = _hydrate(conn, set(body.selected))
            return result
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)


class SimAdvise(BaseModel):
    program_id: str = "2559"
    selected: list[str] = []
    chosen_plans: list[str] = []
    branch: list[str] = []
    goal: str
    generate: bool = True


@router.post("/advise")
def sim_advise_ep(body: SimAdvise):
    """AI course advice: deterministic candidate pool (enumerated available ∪ countable E/F), LLM only ranks and explains + dual guardrails."""
    try:
        with psycopg.connect(DSN) as conn:
            conn.read_only = True
            try:
                res = sim_advise.advise(
                    conn, body.program_id, body.goal, selected=body.selected,
                    chosen_plans=body.chosen_plans, branch=body.branch,
                    generate=body.generate)
            except ValueError as e:
                return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=400)
            res["offerings"] = _offerings(conn, [c["code"] for c in res["candidates"]])
            return res
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
