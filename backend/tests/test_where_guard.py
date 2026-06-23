"""WHERE injection-safety regression (pure functions, no DB): after slotting, injection safety is "structural".

The old guard_where/_clean_where blocked injection by scanning a free WHERE string; now the LLM only fills typed slots,
the WHERE is assembled by build_where — column names come from a code-side closed set, all values go into params (psycopg %s),
no free string enters SQL, and there is no SQL text to sanitize. This file locks in that safety invariant (replacing the old two-layer defense):
  - malicious/arbitrary values only land in params, never appear in the SQL text;
  - column names the LLM hallucinates (requirement_type / free-SQL leftovers) are deterministically dropped by _validate_filters.
Slot-shape correctness is covered in test_planner_slots.py; here we only check the safety property.
"""
from app.services import planner
from app.services.retrieval import build_where
from app.services.planner import _validate_filters

INJECTION = "St Lucia'; DROP TABLE courses; --"


def test_malicious_value_stays_in_params_never_in_sql():
    # The injection string as a location value: it can only go into params, the SQL text has only the placeholder %s
    sql, params = build_where({"location": INJECTION})
    assert sql == "location = %s"
    assert params == [INJECTION]
    assert INJECTION not in sql and ";" not in sql and "DROP" not in sql.upper()


def test_course_type_list_value_parameterized():
    # List values (NOT IN / IN) are also parameterized: values go into params, SQL has only the %s array placeholder
    sql, params = build_where({"course_type_exclude": ["thesis'; DROP", "research"]})
    assert sql == "course_type <> ALL(%s)"
    assert params == [["thesis'; DROP", "research"]]
    assert "DROP" not in sql.upper()


def test_validate_drops_hallucinated_columns():
    # LLM-hallucinated columns / free-SQL leftover keys are all dropped, never enter build_where (replaces _clean_where's whole-clause wipe)
    assert _validate_filters(
        {"requirement_type": "core", "title": "%ml%",
         "has_exam=false; drop table courses": 1}) == {}
    # Keep valid slots, remove hallucinated keys (mixed scenario)
    assert _validate_filters({"has_exam": False, "requirement_type": "thesis"}) == \
        {"has_exam": False}


def test_validate_rejects_out_of_enum_level(monkeypatch):
    # level is validated against the real enum; a non-existent level value the LLM invents is dropped (never assembled into SQL)
    monkeypatch.setitem(planner._ENUM_CACHE, "level",
                        {"undergraduate", "postgraduate coursework"})
    assert _validate_filters({"level": "Master"}) == {}
    assert _validate_filters({"level": "Postgraduate Coursework"}) == \
        {"level": "Postgraduate Coursework"}


def test_build_where_only_emits_closed_set_columns():
    # Even if an unknown key is stuffed into the validated dict (defensive), build_where only recognizes _WHERE_BUILDERS / course_type_*,
    # an unknown key produces no SQL fragment (closed-set column names, structural safety)
    sql, params = build_where({"has_exam": False, "evil_col": "x", "drop": 1})
    assert sql == "has_exam = %s" and params == [False]
