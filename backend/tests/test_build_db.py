"""年课 / 课程类型 / 期中考试派生标记回归(纯函数,无 DB)。"""
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
    # REIT7841:title 无 thesis 字样,但有一项考核 category 单独是 Thesis
    assert classify_course_type(
        "Research and Development Methods and Practice", _ass("Quiz", "Thesis")
    ) == "thesis"


def test_course_type_multivalue_thesis_category_is_not_thesis():
    # 'Project, Thesis' 这类多值串里夹一个 Thesis 提交格式不足以判论文课(STAT3008 误标回归)
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
    # 纯 assessment 的 Placement 类别噪声大:正常授课课带实习考核环节不应判 placement
    assert classify_course_type("Paediatric Audiology I", _ass("Examination", "Placement")) == "coursework"


def test_course_type_research_needs_project_context():
    assert classify_course_type("Major Research Project & Seminar", None) == "research"
    assert classify_course_type("Honours Research", None) == "research"
    # 「讲授研究方法 / 实验室技能」类课无 project/honours 上下文 -> 不误判成 research
    assert classify_course_type("Principles of Biomedical Research", None) == "coursework"
    assert classify_course_type("Advanced Research Methodologies", None) == "coursework"
    assert classify_course_type("Laboratory Skills in Genetic Research", None) == "coursework"


def test_course_type_priority_and_default():
    # 优先级 placement > thesis/research
    assert classify_course_type("Industry Research Placement", _ass("Thesis")) == "placement"
    assert classify_course_type("Introduction to Software Engineering", _ass("Examination")) == "coursework"
    assert classify_course_type(None, None) == "coursework"


def test_year_long_true_when_span_crosses_two_semesters():
    assert is_year_long("Semester 1, 2026 (23/02/2026 - 21/11/2026)") is True
    assert is_year_long("Semester 1, 2026 (23/02/2026 - 20/11/2026)") is True


def test_standard_semester_is_not_year_long():
    assert is_year_long("Semester 1, 2026 (23/02/2026 - 20/06/2026)") is False
    # 偏长的 S1(到 7 月初)仍非年课
    assert is_year_long("Semester 1, 2026 (27/01/2026 - 10/07/2026)") is False
    # 标准 S2 课(约 4 个月)不得被误判为年课
    assert is_year_long("Semester 2, 2026 (27/07/2026 - 20/11/2026)") is False


def test_unparseable_study_period_returns_none():
    assert is_year_long(None) is None
    assert is_year_long("") is None
    assert is_year_long("Teaching Period 3") is None


def test_midterm_has_from_in_semester_or_midterm_naming():
    # UQ 期中标准命名是 In-Semester Exam(极少用 Midterm),两者都判 has
    assert classify_midterm(_ex("In-Semester Exam", "Final Exam")) == "has"
    assert classify_midterm(_ex("Mid-Semester Exam")) == "has"
    assert classify_midterm(_ex("Midterm Exam")) == "has"


def test_midterm_has_includes_in_class_quiz_even_non_exam_category():
    # in-class 课堂测验按业务规则计入期中(category 非 Examination 也算)
    assert classify_midterm([{"task": "In class Quiz (3 quizzes)", "category": "Quiz"}]) == "has"


def test_midterm_none_when_final_only_or_no_exam():
    assert classify_midterm(_ex("Final Exam")) == "none"
    assert classify_midterm(_ex("End of Semester Exam")) == "none"
    # 无 exam/quiz/test 类考核 -> 确定没有期中考试
    assert classify_midterm(_ass("Essay/ Critique", "Presentation")) == "none"
    assert classify_midterm(None) == "none"
    assert classify_midterm([]) == "none"


def test_midterm_unknown_when_exam_naming_has_no_timepoint():
    # 有考试但命名判不出时点 -> unknown,绝不静默当成 none(refuse over wrong)
    assert classify_midterm(_ex("Exam")) == "unknown"
    assert classify_midterm(_ex("Examination")) == "unknown"
    assert classify_midterm([{"task": "Quiz Online", "category": "Quiz"}]) == "unknown"
    # 期末明确 + 另有判不出时点的考试 -> 仍 unknown(不能确认无期中)
    assert classify_midterm(_ex("Final Exam", "Exam")) == "unknown"


def test_group_has_from_standard_marker_or_group_words():
    # UQ 标准标记 "Team or group-based"(取自真实 ENVM3103 / COMP3880 考核)
    assert classify_group([{"task": "EPBC Act Report Team or group-based", "category": "Paper"}]) == "has"
    assert classify_group([{"task": "Initial Prototype Demonstration  Team or group-based",
                            "category": "Project"}]) == "has"
    # 没打标准标记但 task 直接写 Group / Team(召回优先,一并算 has)
    assert classify_group([{"task": "Group Project Research Plan", "category": "Project"}]) == "has"
    assert classify_group([{"task": "Team Presentation", "category": "Presentation"}]) == "has"


def test_group_none_when_assessments_present_without_group_signal():
    assert classify_group(_ass("Essay/ Critique", "Presentation")) == "none"
    assert classify_group([{"task": "Final Exam", "category": "Examination"}]) == "none"


def test_group_unknown_when_no_assessment_data():
    # 无考核数据判不出 -> unknown,绝不静默当成 none(refuse over wrong)
    assert classify_group(None) == "unknown"
    assert classify_group([]) == "unknown"


def test_apply_exam_override_changes_and_reports_fields():
    # 自动判 unknown 的课经 ECP 核实有期中 -> 覆盖 midterm_status,返回改动字段
    c = {"offering_id": "X-1-1", "has_exam": False, "midterm_status": "unknown"}
    assert apply_exam_override(c, {"midterm_status": "has"}) == ["midterm_status"]
    assert c["midterm_status"] == "has"
    # 核实有期末(自动漏判 has_exam)-> 两列都覆盖
    c2 = {"offering_id": "Y-1-1", "has_exam": False, "midterm_status": "unknown"}
    assert set(apply_exam_override(c2, {"has_exam": True, "midterm_status": "none"})) == {
        "has_exam", "midterm_status"}
    assert c2["has_exam"] is True and c2["midterm_status"] == "none"


def test_apply_exam_override_noop_when_values_match():
    # 值与现有相同不算改动(供命中计数区分真实修正)
    c = {"offering_id": "Z-1-1", "has_exam": False, "midterm_status": "none"}
    assert apply_exam_override(c, {"has_exam": False, "midterm_status": "none"}) == []
    # 只动 has_exam / midterm_status,忽略 override 里的其它键(reason/source 等)
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
    # 文件缺失 -> 空 dict(override 可选)
    assert load_exam_overrides(str(tmp_path / "missing.jsonl")) == {}
    # 缺 offering_id -> 抛错,不静默
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"midterm_status": "none"}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_exam_overrides(str(bad))
