"""年课 / 课程类型派生标记回归(纯函数,无 DB)。"""
from app.pipelines.build_db import classify_course_type, is_year_long


def _ass(*cats):
    return [{"task": "t", "category": c} for c in cats]


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
