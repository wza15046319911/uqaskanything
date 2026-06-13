"""年课派生标记解析回归(纯函数,无 DB)。"""
from app.pipelines.build_db import is_year_long


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
