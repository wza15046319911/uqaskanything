"""Regression for derived flags: year-long course / course type / midterm exam (pure functions, no DB)."""
import json

import pytest

from app.pipelines.build_db import (
    apply_exam_override,
    classify_course_type,
    classify_group,
    classify_midterm,
    is_year_long,
    load_exam_overrides,
)


def _ass(*cats):
    return [{"task": "t", "category": c} for c in cats]


def _ex(*tasks):
    return [{"task": t, "category": "Examination"} for t in tasks]


def test_course_type_thesis_from_title_or_standalone_thesis_category():
    assert classify_course_type("Research Thesis", None) == "thesis"
    assert classify_course_type("Honours Dissertation", []) == "thesis"
    # REIT7841: title has no "thesis" word, but one assessment category is exactly Thesis
    assert classify_course_type(
        "Research and Development Methods and Practice", _ass("Quiz", "Thesis")
    ) == "thesis"


def test_course_type_multivalue_thesis_category_is_not_thesis():
    # A multi-value string like 'Project, Thesis' that includes a Thesis submission format is not enough to classify a thesis course (STAT3008 mislabel regression)
    assert classify_course_type(
        "Selected Topics in Statistical Learning",
        _ass("Tutorial/ Problem Set", "Project, Thesis", "Examination"),
    ) == "coursework"
    assert classify_course_type(
        "Critical Issues in Finance", _ass("Paper/ Report/ Annotation, Thesis")
    ) == "coursework"


def test_course_type_placement_only_from_title_not_assessment():
    assert classify_course_type("Engineering Placement A", None) == "placement"
    assert classify_course_type("Communication Internship", None) == "placement"
    # A Placement category from assessment alone is noisy: a normal taught course with an internship assessment item should not be classified as placement
    assert classify_course_type("Paediatric Audiology I", _ass("Examination", "Placement")) == "coursework"


def test_course_type_research_needs_project_context():
    assert classify_course_type("Major Research Project & Seminar", None) == "research"
    assert classify_course_type("Honours Research", None) == "research"
    # Courses that "teach research methods / lab skills" with no project/honours context -> not wrongly classified as research
    assert classify_course_type("Principles of Biomedical Research", None) == "coursework"
    assert classify_course_type("Advanced Research Methodologies", None) == "coursework"
    assert classify_course_type("Laboratory Skills in Genetic Research", None) == "coursework"


def test_course_type_priority_and_default():
    # Priority placement > thesis/research
    assert classify_course_type("Industry Research Placement", _ass("Thesis")) == "placement"
    assert classify_course_type("Introduction to Software Engineering", _ass("Examination")) == "coursework"
    assert classify_course_type(None, None) == "coursework"


def test_year_long_true_when_span_crosses_two_semesters():
    assert is_year_long("Semester 1, 2026 (23/02/2026 - 21/11/2026)") is True
    assert is_year_long("Semester 1, 2026 (23/02/2026 - 20/11/2026)") is True


def test_standard_semester_is_not_year_long():
    assert is_year_long("Semester 1, 2026 (23/02/2026 - 20/06/2026)") is False
    # A longer-than-usual S1 (into early July) is still not a year-long course
    assert is_year_long("Semester 1, 2026 (27/01/2026 - 10/07/2026)") is False
    # A standard S2 course (about 4 months) must not be wrongly classified as year-long
    assert is_year_long("Semester 2, 2026 (27/07/2026 - 20/11/2026)") is False


def test_unparseable_study_period_returns_none():
    assert is_year_long(None) is None
    assert is_year_long("") is None
    assert is_year_long("Teaching Period 3") is None


def test_midterm_has_from_in_semester_or_midterm_naming():
    # UQ's standard midterm naming is In-Semester Exam (Midterm is rare); both count as has
    assert classify_midterm(_ex("In-Semester Exam", "Final Exam")) == "has"
    assert classify_midterm(_ex("Mid-Semester Exam")) == "has"
    assert classify_midterm(_ex("Midterm Exam")) == "has"


def test_midterm_has_includes_in_class_quiz_even_non_exam_category():
    # An in-class quiz counts as midterm per business rule (even if the category is not Examination)
    assert classify_midterm([{"task": "In class Quiz (3 quizzes)", "category": "Quiz"}]) == "has"


def test_midterm_none_when_final_only_or_no_exam():
    assert classify_midterm(_ex("Final Exam")) == "none"
    assert classify_midterm(_ex("End of Semester Exam")) == "none"
    # No exam/quiz/test assessment -> definitely no midterm exam
    assert classify_midterm(_ass("Essay/ Critique", "Presentation")) == "none"
    assert classify_midterm(None) == "none"
    assert classify_midterm([]) == "none"


def test_midterm_unknown_when_exam_naming_has_no_timepoint():
    # Has an exam but the naming gives no time point -> unknown, never silently treat as none (refuse over wrong)
    assert classify_midterm(_ex("Exam")) == "unknown"
    assert classify_midterm(_ex("Examination")) == "unknown"
    assert classify_midterm([{"task": "Quiz Online", "category": "Quiz"}]) == "unknown"
    # A clear final + another exam with no clear time point -> still unknown (cannot confirm there is no midterm)
    assert classify_midterm(_ex("Final Exam", "Exam")) == "unknown"


def test_group_has_from_standard_marker_or_group_words():
    # UQ standard marker "Team or group-based" (taken from real ENVM3103 / COMP3880 assessments)
    assert classify_group([{"task": "EPBC Act Report Team or group-based", "category": "Paper"}]) == "has"
    assert classify_group([{"task": "Initial Prototype Demonstration  Team or group-based",
                            "category": "Project"}]) == "has"
    # No standard marker but the task says Group / Team directly (recall first, also count as has)
    assert classify_group([{"task": "Group Project Research Plan", "category": "Project"}]) == "has"
    assert classify_group([{"task": "Team Presentation", "category": "Presentation"}]) == "has"


def test_group_none_when_assessments_present_without_group_signal():
    assert classify_group(_ass("Essay/ Critique", "Presentation")) == "none"
    assert classify_group([{"task": "Final Exam", "category": "Examination"}]) == "none"


def test_group_unknown_when_no_assessment_data():
    # No assessment data, cannot decide -> unknown, never silently treat as none (refuse over wrong)
    assert classify_group(None) == "unknown"
    assert classify_group([]) == "unknown"


def test_apply_exam_override_changes_and_reports_fields():
    # A course auto-classified as unknown is verified via ECP to have a midterm -> override midterm_status, return changed fields
    c = {"offering_id": "X-1-1", "has_exam": False, "midterm_status": "unknown"}
    assert apply_exam_override(c, {"midterm_status": "has"}) == ["midterm_status"]
    assert c["midterm_status"] == "has"
    # Verified to have a final (has_exam was auto-missed) -> override both columns
    c2 = {"offering_id": "Y-1-1", "has_exam": False, "midterm_status": "unknown"}
    assert set(apply_exam_override(c2, {"has_exam": True, "midterm_status": "none"})) == {
        "has_exam", "midterm_status"}
    assert c2["has_exam"] is True and c2["midterm_status"] == "none"


def test_apply_exam_override_noop_when_values_match():
    # A value equal to the existing one does not count as a change (lets the hit count distinguish real fixes)
    c = {"offering_id": "Z-1-1", "has_exam": False, "midterm_status": "none"}
    assert apply_exam_override(c, {"has_exam": False, "midterm_status": "none"}) == []
    # Only touch has_exam / midterm_status, ignore other keys in override (reason/source etc.)
    c2 = {"offering_id": "Z-2-2", "has_exam": False, "midterm_status": "unknown"}
    assert apply_exam_override(c2, {"midterm_status": "none", "reason": "x"}) == ["midterm_status"]
    assert "reason" not in c2


def test_load_exam_overrides_parses_and_errors(tmp_path):
    p = tmp_path / "ov.jsonl"
    p.write_text(
        json.dumps({"offering_id": "A-1-1", "midterm_status": "has"}) + "\n\n"
        + json.dumps({"offering_id": "B-2-2", "has_exam": True}) + "\n",
        encoding="utf-8")
    ov = load_exam_overrides(str(p))
    assert set(ov) == {"A-1-1", "B-2-2"} and ov["A-1-1"]["midterm_status"] == "has"
    # Missing file -> empty dict (override is optional)
    assert load_exam_overrides(str(tmp_path / "missing.jsonl")) == {}
    # Missing offering_id -> raise, do not stay silent
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"midterm_status": "none"}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_exam_overrides(str(bad))
