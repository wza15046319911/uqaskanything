"""单课子问题(先修/考核/学分/开课)确定性作答:意图判定 + 结构化字段渲染。无 DB / 无 LLM。"""
from app.services import answer as answer_mod
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


# 仿真实:DECO3800 含 presentation 考核;CSSE1001 无;DECO_NO_DATA 无结构化考核。
DECO3800 = {
    "code": "DECO3800",
    "title": "Design Computing Studio 3 - Build",
    "assessments": [
        {"task": "Studio Portfolio", "category": "Portfolio", "weight": 50.0, "hurdle": True},
        {"task": "Final Oral Presentation", "category": "Presentation", "weight": 30.0},
    ],
}
DECO_NO_DATA = {"code": "DECO9999", "title": "Ghost Course", "assessments": []}


def test_assessment_type_present_lists_matched_items():
    ans = detail_structured_answer("DECO3800 有没有 presentation", DECO3800)
    assert ans is not None
    assert "有演讲/展示类考核" in ans
    assert "Final Oral Presentation" in ans and "30%" in ans
    assert "Studio Portfolio" not in ans          # 只列命中项,不混入其他考核


def test_assessment_type_present_chinese_question():
    ans = detail_structured_answer("DECO3800 这门课要演讲吗", DECO3800)
    assert ans is not None and "Final Oral Presentation" in ans


def test_assessment_type_absent_says_no():
    ans = detail_structured_answer("CSSE1001 有 presentation 吗", CSSE1001)
    assert ans is not None
    assert "没有演讲/展示类考核" in ans


def test_assessment_type_no_data_is_unknown_not_no():
    # 无结构化考核数据时归 unknown,绝不静默当「没有」(refuse over wrong)
    ans = detail_structured_answer("DECO9999 有 presentation 吗", DECO_NO_DATA)
    assert ans is not None
    assert "无法确认" in ans and "没有" not in ans


def test_assessment_type_takes_precedence_over_generic_list():
    # 同时含类型词与「占比」时,先答具体类型而非列全部考核
    ans = detail_structured_answer("DECO3800 presentation 占比多少", DECO3800)
    assert "有演讲/展示类考核" in ans
    assert "Studio Portfolio" not in ans


def test_seeded_type_portfolio_present():
    ans = detail_structured_answer("DECO3800 有没有作品集", DECO3800)
    assert "有作品集类考核" in ans and "Studio Portfolio" in ans


def test_seeded_type_quiz_absent():
    ans = detail_structured_answer("CSSE1001 有 quiz 吗", CSSE1001)
    assert "没有测验类考核" in ans


def test_catchall_feeds_question_and_full_record_to_llm(monkeypatch):
    # 非关键词长尾问题 -> 走 LLM 兜底,且学生问题 + 完整记录(考核/先修)都进 prompt
    captured = {}

    def fake_call(messages):
        captured["user"] = messages[-1]["content"]
        return "这门课的难度因人而异。"

    monkeypatch.setattr(answer_mod.llm, "call", fake_call)
    course = {**CSSE1001, "description": "Intro course.", "prerequisite_raw": "MATH1051"}
    ans = answer_course_detail("CSSE1001 这门课难吗", course)
    u = captured["user"]
    assert "这门课难吗" in u                                   # 学生问题进 prompt
    assert "Assignment 1" in u and "In-semester exam" in u      # 完整考核进上下文
    assert "MATH1051" in u                                      # 先修原文进上下文
    assert "这门课的难度因人而异。" in ans


def test_answer_course_detail_missing_course():
    assert "未找到该课程" in answer_course_detail("CSSE9999 的先修课", None)


def test_general_intro_appends_assessment(monkeypatch):
    # 通用「介绍这门课」要在简介末尾追加确定性考核组成(考核字段直出,不经 LLM)
    monkeypatch.setattr(answer_mod.llm, "call", lambda messages: "这是一门软件工程入门课。")
    course = {**CSSE1001, "description": "Intro to software engineering."}
    ans = answer_course_detail("详细介绍 CSSE1001", course)
    assert "这是一门软件工程入门课。" in ans
    assert "考核组成" in ans
    assert "Assignment 1" in ans and "15%" in ans
    assert "In-semester exam" in ans and "hurdle" in ans


def test_general_intro_no_assessment_data_no_appendix(monkeypatch):
    # 无结构化考核数据时通用简介不追加,也不出「暂无」占位
    monkeypatch.setattr(answer_mod.llm, "call", lambda messages: "微积分与线性代数入门。")
    course = {**MATH1051, "description": "Calculus and linear algebra."}
    ans = answer_course_detail("介绍一下 MATH1051", course)
    assert ans == "微积分与线性代数入门。"
    assert "考核组成" not in ans and "暂无结构化考核信息" not in ans


def test_struct_context_carries_answer_fields():
    # llm_judge faithfulness 依据须含答案引用的字段值
    ctx = _detail_struct_context(MATH1051, ["prereq", "assessment"])
    joined = "\n".join(ctx)
    assert "prerequisite_raw=" in joined and "MATH1050" in joined
    assert "assessments=" in joined
