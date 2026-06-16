"""单课子问题(先修/考核/学分/开课)确定性作答:意图判定 + 结构化字段渲染。无 DB / 无 LLM。"""
from app.services.answer import (
    _detail_intents,
    detail_structured_answer,
    answer_course_detail,
    _detail_struct_context,
)

# 仿真实 course_detail 返回:CSSE1001 无先修(空串),MATH1051 有先修。
CSSE1001 = {
    "code": "CSSE1001",
    "title": "Introduction to Software Engineering",
    "units": 2.0,
    "prerequisite_raw": "",
    "semesters": ["S1", "S2"],
    "locations": ["St Lucia"],
    "assessments": [
        {"task": "Assignment 1", "hurdle": False, "weight": 15.0},
        {"task": "In-semester exam", "hurdle": True, "weight": 25.0},
    ],
}
MATH1051 = {
    "code": "MATH1051",
    "title": "Calculus & Linear Algebra I",
    "units": 2.0,
    "prerequisite_raw": "MATH1050 or a grade of C or higher in Queensland Year 12 Specialist Mathematics.",
    "semesters": ["S1", "S2"],
    "locations": ["St Lucia"],
    "assessments": [],
}


def test_intent_general_intro_is_empty():
    # 「讲什么 / 介绍」不命中任何结构化子问题 -> 交回 LLM 简介
    assert _detail_intents("CSSE1001 讲什么") == []
    assert _detail_intents("介绍一下 CSSE1001 这门课") == []


def test_intent_single_match():
    assert _detail_intents("CSSE1001 的先修课是什么") == ["prereq"]
    assert _detail_intents("CSSE1001 怎么考核") == ["assessment"]
    assert _detail_intents("CSSE1001 几个学分") == ["units"]
    assert _detail_intents("CSSE1001 哪个学期开") == ["semester"]


def test_intent_compound_preserves_order():
    # 复合问题按 prereq -> assessment -> units -> semester 固定顺序
    assert _detail_intents("CSSE1001 的先修和考核分别是什么") == ["prereq", "assessment"]


def test_prereq_empty_says_no_prereq():
    ans = detail_structured_answer("CSSE1001 的先修课是什么", CSSE1001)
    assert ans is not None
    assert "CSSE1001" in ans
    assert "没有先修课要求" in ans


def test_prereq_present_quotes_raw_verbatim():
    ans = detail_structured_answer("MATH1051 的先修课是什么", MATH1051)
    # 高成本事实:先修逻辑原文逐字给出(仅去尾句号),不交 LLM 改写
    assert MATH1051["prerequisite_raw"].rstrip("。.") in ans
    assert "MATH1051" in ans


def test_assessment_lists_tasks_with_weight_and_hurdle():
    ans = detail_structured_answer("CSSE1001 怎么考核", CSSE1001)
    assert "Assignment 1" in ans and "15%" in ans
    assert "In-semester exam" in ans and "25%" in ans
    assert "hurdle" in ans          # In-semester exam 是 hurdle


def test_assessment_missing_data_is_explicit_not_intro():
    ans = detail_structured_answer("MATH1051 怎么考核", MATH1051)
    assert "暂无结构化考核信息" in ans


def test_units_renders_integer_without_trailing_zero():
    ans = detail_structured_answer("CSSE1001 几学分", CSSE1001)
    assert "2 学分" in ans
    assert "2.0" not in ans


def test_semester_lists_semesters_and_campus():
    ans = detail_structured_answer("CSSE1001 哪个学期开", CSSE1001)
    assert "S1" in ans and "S2" in ans
    assert "St Lucia" in ans


def test_general_question_returns_none_for_llm_path():
    assert detail_structured_answer("CSSE1001 讲什么", CSSE1001) is None


def test_answer_course_detail_missing_course():
    assert "未找到该课程" in answer_course_detail("CSSE9999 的先修课", None)


def test_struct_context_carries_answer_fields():
    # llm_judge faithfulness 依据须含答案引用的字段值
    ctx = _detail_struct_context(MATH1051, ["prereq", "assessment"])
    joined = "\n".join(ctx)
    assert "prerequisite_raw=" in joined and "MATH1050" in joined
    assert "assessments=" in joined
