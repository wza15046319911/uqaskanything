"""planner 确定性 level 兜底回归(纯函数,无 DB)。

覆盖 master/bachelor 当层级用,以及"Master of X"(program 名)不被误当层级。
"""
from app.services.planner import _enforce_level_hint

PG = "level='Postgraduate Coursework'"
UG = "level='Undergraduate'"


def test_master_bachelor_map_to_level():
    assert _enforce_level_hint("has_exam=false", "Master没考试的课") == f"has_exam=false AND {PG}"
    assert _enforce_level_hint("", "bachelor 的课") == UG
    assert _enforce_level_hint("", "硕士课程") == PG
    assert _enforce_level_hint("", "学士课程") == UG
    # 既有的研究生/本科继续生效
    assert _enforce_level_hint("", "研究生课") == PG
    assert _enforce_level_hint("", "undergraduate courses") == UG


def test_program_name_of_form_not_treated_as_level():
    # "Master of X" / "Bachelor of X" 是专业名,不该被兜底成 level(避免污染 program 查询)
    assert _enforce_level_hint("", "Master of Data Science 的课") == ""
    assert _enforce_level_hint("has_exam=false", "Bachelor of Computer Science") == "has_exam=false"


def test_keyword_overrides_wrong_llm_level():
    # 问题含层级词时确定性值为准:LLM 把 bachelor 错映射成 Postgraduate 也要被纠正(规则 12)
    assert _enforce_level_hint(PG, "bachelor没考试的课") == UG
    assert _enforce_level_hint(f"{PG} AND has_exam=false", "bachelor没考试的课") == f"{UG} AND has_exam=false"


def test_no_keyword_respects_llm_level_and_unchanged():
    # 无我方层级关键词(如 honours)时尊重 LLM 已写的 level
    assert _enforce_level_hint(UG, "honours 课") == UG
    # 无层级关键词不动 where
    assert _enforce_level_hint("has_exam=false", "没考试的课") == "has_exam=false"
