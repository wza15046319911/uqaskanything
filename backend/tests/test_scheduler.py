"""排课回归:拓扑序 / cap / offering 钉死 / 环 / 零静默丢。无 DB 依赖。"""
import scheduler


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
