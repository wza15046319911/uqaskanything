"""planner deterministic level fallback regression (pure functions, no DB).

Covers master/bachelor used as a level, and "Master of X" (a program name) not being wrongly treated as a level.
"""
from app.services import planner
from app.services.planner import (
    _enforce_level_hint, _program_filter_where, _force_program_route,
    _expand_program_abbr, _code_level_digits, _faculty_units,
    _validate_coord_unit, _both_semesters_intent,
    _excluded_title_kw, plan)

PG = "Postgraduate Coursework"
UG = "Undergraduate"


def test_master_bachelor_map_to_level():
    # After slotting, _enforce_level_hint takes/returns a filters dict: matching a level word -> write the level slot
    assert _enforce_level_hint({"has_exam": False}, "Master没考试的课") == \
        {"has_exam": False, "level": PG}
    assert _enforce_level_hint({}, "bachelor 的课") == {"level": UG}
    assert _enforce_level_hint({}, "硕士课程") == {"level": PG}
    assert _enforce_level_hint({}, "学士课程") == {"level": UG}
    # The existing postgraduate/undergraduate keywords still work
    assert _enforce_level_hint({}, "研究生课") == {"level": PG}
    assert _enforce_level_hint({}, "undergraduate courses") == {"level": UG}


def test_program_name_of_form_not_treated_as_level():
    # "Master of X" / "Bachelor of X" is a program name, should not fall back to a level (avoid polluting the program query)
    assert _enforce_level_hint({}, "Master of Data Science 的课") == {}
    assert _enforce_level_hint({"has_exam": False}, "Bachelor of Computer Science") == \
        {"has_exam": False}


def test_keyword_overrides_wrong_llm_level():
    # When the question has a level word, the deterministic value wins: even if the LLM wrongly maps bachelor to Postgraduate it must be corrected (rule 12)
    assert _enforce_level_hint({"level": PG}, "bachelor没考试的课") == {"level": UG}
    assert _enforce_level_hint({"level": PG, "has_exam": False}, "bachelor没考试的课") == \
        {"level": UG, "has_exam": False}


def test_no_keyword_respects_llm_level_and_unchanged():
    # When there is no level keyword of ours (e.g. honours), respect the level the LLM already wrote
    assert _enforce_level_hint({"level": UG}, "honours 课") == {"level": UG}
    # No level keyword, do not touch filters
    assert _enforce_level_hint({"has_exam": False}, "没考试的课") == {"has_exam": False}


def test_program_filter_where_rebuilds_structured_conditions():
    # Deterministic mapping of has/no exam (negation first) -> filters dict
    assert _program_filter_where("没有考试的课") == {"has_exam": False}
    assert _program_filter_where("有考试的课") == {"has_exam": True}
    # Units + exam combined (units normalized to a number)
    assert _program_filter_where("2学分没考试的课") == {"has_exam": False, "units": 2}
    # Exclude course types (deduped, ascending)
    assert _program_filter_where("不含thesis和placement的课") == \
        {"course_type_exclude": ["placement", "thesis"]}
    # Deterministic mapping of has/no group assessment (negation first, Chinese and English keywords)
    assert _program_filter_where("没有小组作业的课") == {"group_status": "none"}
    assert _program_filter_where("没有 group project 的课") == {"group_status": "none"}
    assert _program_filter_where("有 groupwork 的课") == {"group_status": "has"}
    # Exam + group combined
    assert _program_filter_where("没考试也没有小组作业的课") == \
        {"has_exam": False, "group_status": "none"}
    # Level words injected via _enforce_level_hint
    assert _program_filter_where("研究生没考试的课") == {"has_exam": False, "level": PG}
    # Semester filter: S2/S1 goes into filters (build_where then routes to the offered_s2/offered_s1 flag)
    assert _program_filter_where("S2开放的没有考试的课") == \
        {"has_exam": False, "semester": "S2"}
    assert _program_filter_where("第一学期的课") == {"semester": "S1"}
    # A "both semesters" universal quantifier does not fall into a single semester (program p2c has no cross-semester conjunction; a single one would answer wrong)
    assert "semester" not in _program_filter_where("S1和S2都没有考试的课")
    # No mappable dimension -> empty dict (falls back to a plain program_to_courses)
    assert _program_filter_where("Bachelor of Commerce 的必修课") == {}


def test_force_program_route_combined_program_filter():
    # Degree name + structured filter intent -> program_to_courses (enables the "program + filter" combined query)
    assert _force_program_route("bachelor of commerce 没有考试的课") == \
        ("program_to_courses", "", "bachelor of commerce")
    # Degree name + course-type / requirement words still take the old path
    assert _force_program_route("Bachelor of Computer Science 要修哪些课") == \
        ("program_to_courses", "", "Bachelor of Computer Science")
    # Filter only, no degree name -> do not force program (still a plain filter)
    assert _force_program_route("没有考试的课") is None


def test_expand_program_abbr():
    # Whole-word expansion of subject abbreviations so ILIKE hits the full name in the DB (DB title is Computer Science, not CS)
    assert _expand_program_abbr("master of CS") == "master of computer science"
    assert _expand_program_abbr("Master of IT") == "Master of information technology"
    assert _expand_program_abbr("bachelor of EE") == "bachelor of electrical engineering"
    # Already a full name / no abbreviation -> unchanged; empty string is safe
    assert _expand_program_abbr("Bachelor of Computer Science") == "Bachelor of Computer Science"
    assert _expand_program_abbr("") == ""
    # Whole-word boundary: do not cut letter groups inside a word (substrings inside Science stay)
    assert _expand_program_abbr("Master of Data Science") == "Master of Data Science"


def test_low_burden_short_circuits_before_llm():
    # "easy / low-effort course" deterministic fast path: short-circuits before the LLM (with schema_doc given, no DB connection / no model call)
    # -> objective workload filter (no exam + no hurdle + exclude project courses) + sort by number of assessments ascending, never letting the LLM judge difficulty.
    p = plan("能躺平的课", schema_doc="x")
    assert p["mode"] == "filter" and p["order"] == "assessments_asc"
    assert p["filters"]["has_exam"] is False and p["filters"]["has_hurdle"] is False
    assert set(p["filters"]["course_type_exclude"]) == {"thesis", "research", "placement"}
    # Level words deterministically merged
    p2 = plan("轻松好过的研究生课", schema_doc="x")
    assert p2["filters"]["level"] == "Postgraduate Coursework" and p2["order"] == "assessments_asc"
    # Other low-effort synonym triggers
    for q in ["作业少的课", "水课推荐", "考核少的课"]:
        assert plan(q, schema_doc="x")["order"] == "assessments_asc", q
    # "simple assessment makeup" anchors the trigger and lands on the same objective-sort fast path (no longer wrongly refused as empty)
    for q in ["assessment组成最简单的课", "考核最简单的课", "考核组成简单的课"]:
        p3 = plan(q, schema_doc="x")
        assert p3["mode"] == "filter" and p3["order"] == "assessments_asc", q
        assert p3["filters"]["has_exam"] is False and p3["filters"]["has_hurdle"] is False, q


def test_code_level_digit_extraction():
    # "filter by year using the first digit of the code": digit + leading/prefix/Xxxx/English "starting with"
    assert _code_level_digits("course code为1或3开头的") == ["1", "3"]
    assert _code_level_digits("3字头的课") == ["3"]
    assert _code_level_digits("starting with 2") == ["2"]
    assert _code_level_digits("1xxx 的课") == ["1"]
    assert _code_level_digits("2、3 打头的研究生课") == ["2", "3"]
    # Prefix style: the digit comes after "leading/prefix/first" (leading is X / starts with X / first is X)
    assert _code_level_digits("course code开头为1或2或3的课") == ["1", "2", "3"]
    assert _code_level_digits("开头是2或3的课") == ["2", "3"]
    assert _code_level_digits("首位为3的课") == ["3"]
    # No code-level intent -> empty; in particular the 1 in 'semester 1' is not wrongly taken as a level (no leading/prefix binding)
    assert _code_level_digits("没有考试的课") == []
    assert _code_level_digits("semester 1 的课") == []


def test_faculty_units_mapping():
    # Subject -> coordinating_unit controlled mapping (deterministic, used to confine semantic recall back to the owning school)
    assert _faculty_units("商科有哪些没考试的课") == ["Business School", "Economics School"]
    # Computing subject words do NOT lock the school: CS courses sit across schools (COSC in Math&Physics, CYBR in Business,
    # DATA in HPI), so hard-locking EECS would wrongly drop them. Always rely on semantic recall; only an explicit school name locks via Option C.
    assert _faculty_units("IT有哪些课没有考试") == []
    assert _faculty_units("软件相关的课") == []
    assert _faculty_units("electrical engineering 的课") == []
    assert _faculty_units("计算机相关、没有hurdle的研究生课") == []
    # AI/ML/data span maths and statistics, do not lock the school (keep wide recall); no subject word also does not lock
    assert _faculty_units("机器学习的课") == []
    assert _faculty_units("没有考试的课") == []


def test_both_semesters_intent():
    # S1 and S2 both appear + a "both" quantifier -> cross-semester conjunction intent
    assert _both_semesters_intent("S1和S2都没有期中考试和期末考试的课") is True
    assert _both_semesters_intent("S1 S2 两个学期都开的课") is True
    assert _both_semesters_intent("courses with no exam in both S1 and S2") is True
    # Missing quantifier (union semantics, still a plain IN) / only one semester named -> not a conjunction
    assert _both_semesters_intent("S1和S2的课") is False
    assert _both_semesters_intent("S2没考试的课") is False
    assert _both_semesters_intent("没有考试的课") is False


def test_excluded_title_kw():
    # Exclude trigger word + property word -> controlled title keywords (capstone/project/review... that the course_type column cannot separate)
    kw = _excluded_title_kw(
        "S1和S2都没有考试的课,不要thesis, project, proposal, "
        "industry placement, research, literature review, review, capstone")
    assert "capstone" in kw and "project" in kw and "proposal" in kw
    assert "review" in kw and "literature review" in kw and "research" in kw
    assert "placement" in kw  # industry placement -> placement/internship/practicum
    # No exclude trigger word -> empty (avoid treating a topic word as an exclusion)
    assert _excluded_title_kw("有capstone和project的课") == []
    # research does not break "postgraduate"; review word boundary (preview does not count)
    assert _excluded_title_kw("不要research的研究生课") == ["research"]
    assert _excluded_title_kw("排除capstone") == ["capstone"]


def test_validate_coord_unit_accepts_only_real_enum(monkeypatch):
    # Option C: the school string the LLM picks must match the real enum verbatim to pass, else drop it (prevents a freely generated spelling -> exact IN matching 0)
    monkeypatch.setitem(planner._ENUM_CACHE, "coordinating_unit",
                        {"elec engineering & comp science school", "business school"})
    # Verbatim match (case-insensitive) -> pass as is
    assert _validate_coord_unit("Elec Engineering & Comp Science School") == \
        "Elec Engineering & Comp Science School"
    assert _validate_coord_unit("business school") == "business school"
    # A string the LLM freely invents, plausible but not in the DB -> drop to empty (do not pass)
    assert _validate_coord_unit("EECS School") == ""
    assert _validate_coord_unit("Electrical Engineering & Computer Science School") == ""
    # Empty/None is safe
    assert _validate_coord_unit("") == ""
    assert _validate_coord_unit(None) == ""


def test_course_detail_without_real_code_demoted(monkeypatch):
    # The LLM wrongly classifies a question with no course code (e.g. "introduce cs se") as course_detail, with an empty course_code.
    # There is no matchable course code in the question, the deterministic override does not fire -> must demote and re-route,
    # never return course_detail + empty code (otherwise qa calls retrieval.course_detail with an empty code -> 500).
    fake = ('{"mode":"course_detail","semantic_query":"","filters":{},'
            '"course_code":"","program_name":"","direction":"","kb_query":""}')
    monkeypatch.setattr(planner, "_call_llm", lambda prompt: fake)
    p = plan("给我介绍一下cs se", schema_doc="x")
    assert p["mode"] != "course_detail"
    # Has a topic word -> demote to semantic and fall back to a semantic query, instead of a direct 500
    assert p["mode"] == "semantic" and p["semantic_query"]
