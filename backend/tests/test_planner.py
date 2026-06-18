"""planner 确定性 level 兜底回归(纯函数,无 DB)。

覆盖 master/bachelor 当层级用,以及"Master of X"(program 名)不被误当层级。
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
    # 槽位化后 _enforce_level_hint 吃/吐 filters dict:命中层级词 -> 写入 level 槽
    assert _enforce_level_hint({"has_exam": False}, "Master没考试的课") == \
        {"has_exam": False, "level": PG}
    assert _enforce_level_hint({}, "bachelor 的课") == {"level": UG}
    assert _enforce_level_hint({}, "硕士课程") == {"level": PG}
    assert _enforce_level_hint({}, "学士课程") == {"level": UG}
    # 既有的研究生/本科继续生效
    assert _enforce_level_hint({}, "研究生课") == {"level": PG}
    assert _enforce_level_hint({}, "undergraduate courses") == {"level": UG}


def test_program_name_of_form_not_treated_as_level():
    # "Master of X" / "Bachelor of X" 是专业名,不该被兜底成 level(避免污染 program 查询)
    assert _enforce_level_hint({}, "Master of Data Science 的课") == {}
    assert _enforce_level_hint({"has_exam": False}, "Bachelor of Computer Science") == \
        {"has_exam": False}


def test_keyword_overrides_wrong_llm_level():
    # 问题含层级词时确定性值为准:LLM 把 bachelor 错映射成 Postgraduate 也要被纠正(规则 12)
    assert _enforce_level_hint({"level": PG}, "bachelor没考试的课") == {"level": UG}
    assert _enforce_level_hint({"level": PG, "has_exam": False}, "bachelor没考试的课") == \
        {"level": UG, "has_exam": False}


def test_no_keyword_respects_llm_level_and_unchanged():
    # 无我方层级关键词(如 honours)时尊重 LLM 已写的 level
    assert _enforce_level_hint({"level": UG}, "honours 课") == {"level": UG}
    # 无层级关键词不动 filters
    assert _enforce_level_hint({"has_exam": False}, "没考试的课") == {"has_exam": False}


def test_program_filter_where_rebuilds_structured_conditions():
    # 有/无考试确定性映射(否定优先)-> filters dict
    assert _program_filter_where("没有考试的课") == {"has_exam": False}
    assert _program_filter_where("有考试的课") == {"has_exam": True}
    # 学分 + 考试叠加(units 归一成数值)
    assert _program_filter_where("2学分没考试的课") == {"has_exam": False, "units": 2}
    # 排除课型(去重升序)
    assert _program_filter_where("不含thesis和placement的课") == \
        {"course_type_exclude": ["placement", "thesis"]}
    # 有/无小组评估确定性映射(否定优先,中英关键词)
    assert _program_filter_where("没有小组作业的课") == {"group_status": "none"}
    assert _program_filter_where("没有 group project 的课") == {"group_status": "none"}
    assert _program_filter_where("有 groupwork 的课") == {"group_status": "has"}
    # 考试 + 小组叠加
    assert _program_filter_where("没考试也没有小组作业的课") == \
        {"has_exam": False, "group_status": "none"}
    # 层级词经 _enforce_level_hint 注入
    assert _program_filter_where("研究生没考试的课") == {"has_exam": False, "level": PG}
    # 学期限定:S2/S1 进 filters(build_where 再路由到 offered_s2/offered_s1 标记)
    assert _program_filter_where("S2开放的没有考试的课") == \
        {"has_exam": False, "semester": "S2"}
    assert _program_filter_where("第一学期的课") == {"semester": "S1"}
    # 「两个学期都」全称量词不落单学期(program p2c 无跨学期合取,落单会答错)
    assert "semester" not in _program_filter_where("S1和S2都没有考试的课")
    # 无任何可映射维度 -> 空 dict(退化为普通 program_to_courses)
    assert _program_filter_where("Bachelor of Commerce 的必修课") == {}


def test_force_program_route_combined_program_filter():
    # 学位名 + 结构化筛选意图 -> program_to_courses(开启「专业 + 筛选」组合查询)
    assert _force_program_route("bachelor of commerce 没有考试的课") == \
        ("program_to_courses", "", "bachelor of commerce")
    # 学位名 + 课型/要求词仍走老路径
    assert _force_program_route("Bachelor of Computer Science 要修哪些课") == \
        ("program_to_courses", "", "Bachelor of Computer Science")
    # 只有筛选、无学位名 -> 不强制 program(仍是普通 filter)
    assert _force_program_route("没有考试的课") is None


def test_expand_program_abbr():
    # 学科缩写整词展开,使 ILIKE 命中库里全称(库 title 是 Computer Science 而非 CS)
    assert _expand_program_abbr("master of CS") == "master of computer science"
    assert _expand_program_abbr("Master of IT") == "Master of information technology"
    assert _expand_program_abbr("bachelor of EE") == "bachelor of electrical engineering"
    # 已是全称/无缩写 -> 原样;空串安全
    assert _expand_program_abbr("Bachelor of Computer Science") == "Bachelor of Computer Science"
    assert _expand_program_abbr("") == ""
    # 整词边界:不切单词内部的字母组合(Science 里的子串不动)
    assert _expand_program_abbr("Master of Data Science") == "Master of Data Science"


def test_low_burden_short_circuits_before_llm():
    # 「躺平/水课」确定性快路径:先于 LLM 短路(给了 schema_doc 则不连 DB/不调模型)
    # -> 客观负担过滤(无考试+无hurdle+排除项目课)+ 按考核项数升序,绝不靠 LLM 判难度。
    p = plan("能躺平的课", schema_doc="x")
    assert p["mode"] == "filter" and p["order"] == "assessments_asc"
    assert p["filters"]["has_exam"] is False and p["filters"]["has_hurdle"] is False
    assert set(p["filters"]["course_type_exclude"]) == {"thesis", "research", "placement"}
    # 层级词确定性合并
    p2 = plan("轻松好过的研究生课", schema_doc="x")
    assert p2["filters"]["level"] == "Postgraduate Coursework" and p2["order"] == "assessments_asc"
    # 其它低负担同义触发词
    for q in ["作业少的课", "水课推荐", "考核少的课"]:
        assert plan(q, schema_doc="x")["order"] == "assessments_asc", q
    # 「assessment/考核 组成简单」锚定触发,落到同一客观排序快路径(不再误拒成 empty)
    for q in ["assessment组成最简单的课", "考核最简单的课", "考核组成简单的课"]:
        p3 = plan(q, schema_doc="x")
        assert p3["mode"] == "filter" and p3["order"] == "assessments_asc", q
        assert p3["filters"]["has_exam"] is False and p3["filters"]["has_hurdle"] is False, q


def test_code_level_digit_extraction():
    # 「按 code 首位数字筛年级」:digit + 开头/字头/Xxxx/英文 starting with
    assert _code_level_digits("course code为1或3开头的") == ["1", "3"]
    assert _code_level_digits("3字头的课") == ["3"]
    assert _code_level_digits("starting with 2") == ["2"]
    assert _code_level_digits("1xxx 的课") == ["1"]
    assert _code_level_digits("2、3 打头的研究生课") == ["2", "3"]
    # 前置写法:数字在「开头/字头/首位」之后(开头为X / 开头是X / 首位为X)
    assert _code_level_digits("course code开头为1或2或3的课") == ["1", "2", "3"]
    assert _code_level_digits("开头是2或3的课") == ["2", "3"]
    assert _code_level_digits("首位为3的课") == ["3"]
    # 无 code 级别意图 -> 空;尤其 'semester 1' 的 1 不被误当级别(无开头/字头绑定)
    assert _code_level_digits("没有考试的课") == []
    assert _code_level_digits("semester 1 的课") == []


def test_faculty_units_mapping():
    # 学科 -> coordinating_unit 受控映射(确定性,用于把语义召回限定回本学院)
    assert _faculty_units("商科有哪些没考试的课") == ["Business School", "Economics School"]
    # 计算机类学科词「不」锁学院:CS 课跨学院挂靠(COSC 在 Math&Physics、CYBR 在 Business、
    # DATA 在 HPI),硬锁 EECS 会误杀。一律靠语义召回,显式点名学院才经 Option C 锁。
    assert _faculty_units("IT有哪些课没有考试") == []
    assert _faculty_units("软件相关的课") == []
    assert _faculty_units("electrical engineering 的课") == []
    assert _faculty_units("计算机相关、没有hurdle的研究生课") == []
    # AI/ML/数据 跨数学统计,不锁学院(保持宽召回);无学科词也不锁
    assert _faculty_units("机器学习的课") == []
    assert _faculty_units("没有考试的课") == []


def test_both_semesters_intent():
    # S1、S2 同时出现 + 「都/both」量词 -> 跨学期合取意图
    assert _both_semesters_intent("S1和S2都没有期中考试和期末考试的课") is True
    assert _both_semesters_intent("S1 S2 两个学期都开的课") is True
    assert _both_semesters_intent("courses with no exam in both S1 and S2") is True
    # 缺量词(并集语义,仍走普通 IN)/ 只点一个学期 -> 非合取
    assert _both_semesters_intent("S1和S2的课") is False
    assert _both_semesters_intent("S2没考试的课") is False
    assert _both_semesters_intent("没有考试的课") is False


def test_excluded_title_kw():
    # 排除触发词 + 性质词 -> 标题受控关键词(course_type 列分不出的 capstone/project/review…)
    kw = _excluded_title_kw(
        "S1和S2都没有考试的课,不要thesis, project, proposal, "
        "industry placement, research, literature review, review, capstone")
    assert "capstone" in kw and "project" in kw and "proposal" in kw
    assert "review" in kw and "literature review" in kw and "research" in kw
    assert "placement" in kw  # industry placement -> placement/internship/practicum
    # 无排除触发词 -> 空(避免把主题词当排除)
    assert _excluded_title_kw("有capstone和project的课") == []
    # research 不误伤"研究生";review 词边界(preview 不算)
    assert _excluded_title_kw("不要research的研究生课") == ["research"]
    assert _excluded_title_kw("排除capstone") == ["capstone"]


def test_validate_coord_unit_accepts_only_real_enum(monkeypatch):
    # Option C:LLM 选出的学院串必须逐字命中真实枚举才放行,否则丢弃(防自由生成的拼写→精确IN命中0)
    monkeypatch.setitem(planner._ENUM_CACHE, "coordinating_unit",
                        {"elec engineering & comp science school", "business school"})
    # 逐字命中(大小写不敏感)-> 原样放行
    assert _validate_coord_unit("Elec Engineering & Comp Science School") == \
        "Elec Engineering & Comp Science School"
    assert _validate_coord_unit("business school") == "business school"
    # LLM 自由编的、看似合理但 DB 没有的串 -> 丢弃成空(不放行)
    assert _validate_coord_unit("EECS School") == ""
    assert _validate_coord_unit("Electrical Engineering & Computer Science School") == ""
    # 空/None 安全
    assert _validate_coord_unit("") == ""
    assert _validate_coord_unit(None) == ""


def test_course_detail_without_real_code_demoted(monkeypatch):
    # LLM 误把无课程码的问题(如「介绍一下 cs se」)判成 course_detail,course_code 为空。
    # 问题里没有可匹配的课程码,确定性 override 不触发 -> 必须降级重路由,
    # 绝不返回 course_detail + 空 code(否则 qa 调 retrieval.course_detail 空码 500)。
    fake = ('{"mode":"course_detail","semantic_query":"","filters":{},'
            '"course_code":"","program_name":"","direction":"","kb_query":""}')
    monkeypatch.setattr(planner, "_call_llm", lambda prompt: fake)
    p = plan("给我介绍一下cs se", schema_doc="x")
    assert p["mode"] != "course_detail"
    # 有主题词 -> 降级到 semantic 并兜底语义词,而不是直接 500
    assert p["mode"] == "semantic" and p["semantic_query"]
