"""Scheduling regression: topological order / cap / offering pinning / cycle / zero silent drop. No DB dependency."""
from app.services import scheduler


def _placed_index(res):
    return {x["code"]: i for i, s in enumerate(res["semesters"]) for x in s["courses"]}


def test_zero_silent_drop():
    sel = [f"AAAA{i:04d}" for i in range(1, 30)]
    res = scheduler.schedule(sel, units_cap=8.0, n_semesters=4)
    placed = {x["code"] for s in res["semesters"] for x in s["courses"]}
    unplaced = {u["code"] for u in res["unplaced"]}
    assert placed | unplaced == set(sel) and not placed & unplaced


def test_prereq_order():
    res = scheduler.schedule(
        ["COMP2000", "COMP1000", "COMP3000"],
        prereq_map={"COMP2000": {"COMP1000"}, "COMP3000": {"COMP2000"}})
    idx = _placed_index(res)
    assert idx["COMP1000"] < idx["COMP2000"] < idx["COMP3000"]


def test_units_cap_never_exceeded():
    sel = [f"XXXX{i:04d}" for i in range(1, 13)]
    res = scheduler.schedule(sel, units_cap=4.0, n_semesters=8)
    for s in res["semesters"]:
        assert s["units"] <= 4.0


def test_offering_pins_semester():
    res = scheduler.schedule(
        ["AAAA1001", "BBBB1001"],
        offering_map={"AAAA1001": {"S2"}, "BBBB1001": {"S1"}})
    idx = _placed_index(res)
    assert idx["AAAA1001"] % 2 == 1, "S2-only 课必须落 S2 学期"
    assert idx["BBBB1001"] % 2 == 0
    for s in res["semesters"]:
        for x in s["courses"]:
            assert x["verified_offering"]


def test_cycle_goes_unplaced():
    res = scheduler.schedule(
        ["AAAA1001", "BBBB1001"],
        prereq_map={"AAAA1001": {"BBBB1001"}, "BBBB1001": {"AAAA1001"}})
    reasons = {u["code"]: u["reason"] for u in res["unplaced"]}
    assert reasons == {"AAAA1001": "prereq_cycle", "BBBB1001": "prereq_cycle"}


def test_out_of_plan_prereq_warned_not_silent():
    res = scheduler.schedule(["AAAA1001"], prereq_map={"AAAA1001": {"ZZZZ9999"}})
    assert any("ZZZZ9999" in w for w in res["warnings"])
    assert _placed_index(res)["AAAA1001"] == 0


def test_incompatible_warns():
    res = scheduler.schedule(
        ["AAAA1001", "BBBB1001"],
        incompatible_map={"AAAA1001": {"BBBB1001"}})
    assert any("互斥" in w for w in res["warnings"])


def test_intake_s2_flips_offering():
    # S2 intake: slot 0 = S2, so S2-only courses land on even slots and S1-only on odd slots (opposite of S1 intake)
    res = scheduler.schedule(
        ["AAAA1001", "BBBB1001"],
        offering_map={"AAAA1001": {"S2"}, "BBBB1001": {"S1"}},
        start_sem="S2")
    idx = _placed_index(res)
    assert idx["AAAA1001"] % 2 == 0, "S2 入学:S2-only 课落偶数格(格0=S2)"
    assert idx["BBBB1001"] % 2 == 1, "S1-only 课落奇数格"
    for s in res["semesters"]:
        for x in s["courses"]:
            assert x["verified_offering"]


def test_year_long_spans_two_consecutive_semesters():
    # A 16u year-long course takes the two slots [S1, S2], units split 8 each; the start slot is S1 (even slot)
    res = scheduler.schedule(
        ["THES7001"], units_map={"THES7001": 16.0},
        offering_map={"THES7001": {"S1"}}, year_long={"THES7001"},
        units_cap=8.0, n_semesters=6)
    starts = [i for i, s in enumerate(res["semesters"])
              for x in s["courses"] if x.get("part") == "start"]
    assert len(starts) == 1
    s = starts[0]
    assert s % 2 == 0, "年课须 S1 起"
    cont = res["semesters"][s + 1]["courses"]
    assert any(x["code"] == "THES7001" and x.get("part") == "continuation" for x in cont)
    assert res["semesters"][s]["units"] == 8.0
    assert res["semesters"][s + 1]["units"] == 8.0
    assert res["semesters"][s]["courses"][0]["full_units"] == 16.0
    assert not res["unplaced"]


def test_year_long_per_semester_load_respects_cap():
    # After splitting it is 8u per slot; adding a normal course must not exceed the cap
    res = scheduler.schedule(
        ["THES7001", "CSSE1001"],
        units_map={"THES7001": 16.0, "CSSE1001": 4.0},
        offering_map={"THES7001": {"S1"}}, year_long={"THES7001"},
        units_cap=8.0, n_semesters=6)
    for s in res["semesters"]:
        assert s["units"] <= 8.0


def test_year_long_unplaced_when_half_exceeds_cap():
    # A 16u year-long course is 8u per slot after splitting; cap=4 cannot fit -> the whole course is unplaced (not silent)
    res = scheduler.schedule(
        ["THES7001"], units_map={"THES7001": 16.0},
        offering_map={"THES7001": {"S1"}}, year_long={"THES7001"},
        units_cap=4.0, n_semesters=6)
    assert {u["code"] for u in res["unplaced"]} == {"THES7001"}


def test_year_long_no_silent_drop():
    sel = ["THES7001", "AAAA1001"]
    res = scheduler.schedule(
        sel, units_map={"THES7001": 16.0, "AAAA1001": 4.0},
        offering_map={"THES7001": {"S1"}}, year_long={"THES7001"},
        units_cap=8.0, n_semesters=6)
    placed = {x["code"] for s in res["semesters"] for x in s["courses"]}
    unplaced = {u["code"] for u in res["unplaced"]}
    assert placed | unplaced == set(sel)
