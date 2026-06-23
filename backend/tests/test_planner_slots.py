"""Slotting refactor step 2 regression (pure functions, no DB): build_where assembly + _validate_filters checks.

build_where is compared against the equivalent logical WHERE of the old guard_where self-test cases (retrieval __main__):
for the same conditions it now produces parameterized (sql_with_%s, params), and injection safety is structural.
_validate_filters is compared against the old _clean_where: drop unknown keys / illegal values, keep the raw location value (Gatton red line).
"""
from app.services import planner
from app.services.retrieval import build_where, describe_where
from app.services.planner import _validate_filters, _as_number


# ---------- build_where: equivalent comparison against the old guard_where must-pass cases ----------

def test_build_where_single_bool():
    # old "has_exam=false"
    assert build_where({"has_exam": False}) == ("has_exam = %s", [False])
    assert build_where({"has_exam": True}) == ("has_exam = %s", [True])


def test_build_where_level_and_units_order_stable():
    # old "level='Postgraduate Coursework' AND units=2"; order fixed by _WHERE_BUILDERS (level before units)
    assert build_where({"level": "Postgraduate Coursework", "units": 2}) == (
        "level = %s AND units = %s", ["Postgraduate Coursework", 2])
    # Input dict order does not affect output order (deterministic)
    assert build_where({"units": 2, "level": "Postgraduate Coursework"}) == (
        "level = %s AND units = %s", ["Postgraduate Coursework", 2])


def test_build_where_location_literal():
    # old "location='St Lucia'"; a non-enum value (Gatton) also goes into params as is -> 0 hits, never substituted
    assert build_where({"location": "St Lucia"}) == ("location = %s", ["St Lucia"])
    assert build_where({"location": "Gatton"}) == ("location = %s", ["Gatton"])


def test_build_where_course_type_exclude_is_not_in_equivalent():
    # old "has_exam=false AND course_type NOT IN ('placement','thesis','research')"
    # <> ALL(array) ≡ NOT IN (course_type is NOT NULL, no three-valued-logic leak)
    sql, params = build_where(
        {"has_exam": False, "course_type_exclude": ["placement", "thesis", "research"]})
    assert sql == "has_exam = %s AND course_type <> ALL(%s)"
    assert params == [False, ["placement", "thesis", "research"]]


def test_build_where_course_type_only_is_in_equivalent():
    # old "course_type='thesis'" (= or IN unified into = ANY array)
    assert build_where({"course_type_only": ["thesis"]}) == (
        "course_type = ANY(%s)", [["thesis"]])
    assert build_where({"course_type_only": ["coursework", "placement"]}) == (
        "course_type = ANY(%s)", [["coursework", "placement"]])


def test_build_where_all_dimensions_order():
    # All dimensions together: order strictly per _WHERE_BUILDERS, semester uses the offered_s* flag (no param), course_type_only before exclude, both at the tail
    sql, params = build_where({
        "has_exam": True, "has_hurdle": False, "midterm_status": "has",
        "group_status": "none", "level": "Undergraduate", "units": 2,
        "location": "St Lucia", "attendance_mode": "In Person", "semester": "S1",
        "course_type_only": ["coursework"], "course_type_exclude": ["thesis"]})
    assert sql == (
        "has_exam = %s AND has_hurdle = %s AND midterm_status = %s AND "
        "group_status = %s AND level = %s AND units = %s AND location = %s AND "
        "attendance_mode = %s AND offered_s1 = TRUE AND course_type = ANY(%s) AND "
        "course_type <> ALL(%s)")
    assert params == [True, False, "has", "none", "Undergraduate", 2, "St Lucia",
                      "In Person", ["coursework"], ["thesis"]]


def test_build_where_semester_routes_to_offered_flag():
    # semester is not a plain column match: S1/S2 routes to the code-derived offered_s1/offered_s2 flag (no %s param),
    # because the semester text column is stored per offering and is unreliable for "offered this term" (see backfill_offerings).
    assert build_where({"semester": "S2"}) == ("offered_s2 = TRUE", [])
    assert build_where({"semester": "S1"}) == ("offered_s1 = TRUE", [])
    # Combined with other dimensions: has_exam goes into params, the semester flag follows without a param
    assert build_where({"has_exam": False, "semester": "S2"}) == (
        "has_exam = %s AND offered_s2 = TRUE", [False])


def test_build_where_empty_is_pure_no_raise():
    # Pure function: empty filters / None returns ("", []), does not raise (whether to tolerate empty is up to the caller)
    assert build_where({}) == ("", [])
    assert build_where(None) == ("", [])
    # Dimensions that are all None / empty list mean "no filter" and do not appear in the fragment
    assert build_where({"has_exam": None, "level": None,
                        "course_type_exclude": [], "course_type_only": []}) == ("", [])


def test_build_where_false_and_zero_not_dropped():
    # is-None check: False / 0 are valid values and must never be dropped as default
    assert build_where({"has_exam": False}) == ("has_exam = %s", [False])
    assert build_where({"units": 0}) == ("units = %s", [0])


def test_build_where_code_level_substring_first_digit():
    # Filter year by the first digit of the code: take the first digit character of the code (equivalent to _first_digit), value goes into params
    assert build_where({"code_level": ["1", "3"]}) == (
        "substring(code from '[0-9]') = ANY(%s)", [["1", "3"]])
    # Combined with other dimensions: the code_level fragment is at the tail (after course_type)
    sql, params = build_where({"has_exam": False, "code_level": ["1"]})
    assert sql == "has_exam = %s AND substring(code from '[0-9]') = ANY(%s)"
    assert params == [False, ["1"]]
    # Empty list = no filter
    assert build_where({"code_level": []}) == ("", [])


# ---------- _validate_filters: compared against the old _clean_where sanitizing semantics ----------

def test_validate_drops_unknown_keys():
    # Unknown keys (LLM-hallucinated columns / free-SQL leftovers) are all dropped and never enter SQL
    out = _validate_filters({"has_exam": False, "title": "%ml%", "drop table": 1,
                             "requirement_type": "thesis"})
    assert out == {"has_exam": False}


def test_validate_bool_type_enforced():
    # A bool slot must be a real bool; non-bool like the string "false"/1 is dropped (rule 19: no silent coerce)
    assert _validate_filters({"has_exam": False, "has_hurdle": True}) == \
        {"has_exam": False, "has_hurdle": True}
    assert _validate_filters({"has_exam": "false"}) == {}
    assert _validate_filters({"has_exam": 1}) == {}
    assert _validate_filters({"has_exam": None}) == {}


def test_validate_tristate():
    assert _validate_filters({"midterm_status": "none", "group_status": "has"}) == \
        {"midterm_status": "none", "group_status": "has"}
    # Case normalized, unknown is valid
    assert _validate_filters({"midterm_status": "NONE"}) == {"midterm_status": "none"}
    assert _validate_filters({"group_status": "unknown"}) == {"group_status": "unknown"}
    # Illegal tri-state value is dropped
    assert _validate_filters({"midterm_status": "maybe"}) == {}


def test_validate_semester_enum():
    assert _validate_filters({"semester": "S1"}) == {"semester": "S1"}
    assert _validate_filters({"semester": "S2"}) == {"semester": "S2"}
    # Non S1/S2 is dropped ("both" is handled by the both_semesters path, not the single semester slot)
    assert _validate_filters({"semester": "S3"}) == {}


def test_validate_level_against_real_enum(monkeypatch):
    # level is validated against the real DB enum (same pattern as _validate_coord_unit): not in the enum -> drop + log
    monkeypatch.setitem(planner._ENUM_CACHE, "level",
                        {"undergraduate", "postgraduate coursework"})
    assert _validate_filters({"level": "Postgraduate Coursework"}) == \
        {"level": "Postgraduate Coursework"}
    assert _validate_filters({"level": "undergraduate"}) == {"level": "undergraduate"}
    # Non-existent level values the LLM hallucinates (Master/PG) are dropped, never enter SQL
    assert _validate_filters({"level": "Master"}) == {}
    assert _validate_filters({"level": "PG"}) == {}


def test_validate_units_numeric():
    assert _validate_filters({"units": 2}) == {"units": 2}
    assert _validate_filters({"units": 2.0}) == {"units": 2}      # integer value collapses to int
    assert _validate_filters({"units": "2"}) == {"units": 2}      # numeric string converted to number
    assert _validate_filters({"units": "abc"}) == {}              # non-numeric dropped
    assert _validate_filters({"units": True}) == {}              # bool is not a number


def test_validate_location_literal_kept_even_if_non_enum():
    # Red line: location/attendance_mode copy the user's raw value, no enum check (Gatton/Online deliberately produce an empty set)
    assert _validate_filters({"location": "Gatton"}) == {"location": "Gatton"}
    assert _validate_filters({"location": "St Lucia"}) == {"location": "St Lucia"}
    assert _validate_filters({"attendance_mode": "Online"}) == {"attendance_mode": "Online"}
    # Empty string / non-string is dropped
    assert _validate_filters({"location": ""}) == {}
    assert _validate_filters({"location": 123}) == {}


def test_validate_course_type_lists_filtered_to_closed_set():
    # Course-type lists filtered to the valid closed set (deduped, ascending), illegal values dropped + logged
    assert _validate_filters({"course_type_exclude": ["thesis", "research", "placement"]}) == \
        {"course_type_exclude": ["placement", "research", "thesis"]}
    assert _validate_filters({"course_type_only": ["coursework"]}) == \
        {"course_type_only": ["coursework"]}
    # Contains an illegal type: keep only the valid ones; all illegal -> the key does not appear
    assert _validate_filters({"course_type_exclude": ["thesis", "bogus"]}) == \
        {"course_type_exclude": ["thesis"]}
    assert _validate_filters({"course_type_exclude": ["bogus"]}) == {}
    # Non-list is dropped
    assert _validate_filters({"course_type_exclude": "thesis"}) == {}


def test_validate_code_level_digit_list():
    # code_level is a digit list (deterministically injected, values must be single chars 1-9, deduped ascending)
    assert _validate_filters({"code_level": ["3", "1", "1"]}) == {"code_level": ["1", "3"]}
    # Illegal elements (multi-digit / non-digit / 0) are dropped; all illegal -> the key does not appear
    assert _validate_filters({"code_level": ["1", "12", "x", "0"]}) == {"code_level": ["1"]}
    assert _validate_filters({"code_level": ["x"]}) == {}
    # Non-list is dropped
    assert _validate_filters({"code_level": "1"}) == {}


def test_describe_where_renders_code_level():
    # describe_where (the readable dual of build_where): code_level renders as a readable year set
    assert describe_where({"code_level": ["1", "3"]}) == "code首位∈{1,3}"
    assert describe_where({"has_exam": False, "code_level": ["1"]}) == \
        "has_exam=false AND code首位∈{1}"


def test_validate_none_and_non_dict_safe():
    assert _validate_filters(None) == {}
    assert _validate_filters("not a dict") == {}
    assert _validate_filters({}) == {}


def test_as_number_helper():
    assert _as_number(2) == 2
    assert _as_number(2.5) == 2.5
    assert _as_number("2") == 2
    assert _as_number("2.5") == 2.5
    assert _as_number("x") is None
    assert _as_number(True) is None
    assert _as_number(None) is None
