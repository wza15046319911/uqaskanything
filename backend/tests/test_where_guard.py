"""WHERE 注入安全回归(纯函数,无 DB):槽位化后注入安全是「结构性」的。

旧 guard_where/_clean_where 靠扫描自由 WHERE 串拦注入;现在 LLM 只填类型化槽位,
WHERE 由 build_where 拼装——列名来自代码侧闭集,值全进 params(psycopg %s),
没有自由字符串进 SQL,无 SQL 文本可净化。本文件锁住这条安全不变量(替代旧双层防护):
  - 恶意/任意值只会落到 params,绝不出现在 SQL 文本里;
  - LLM 脑补的列名(requirement_type / 自由 SQL 残留)被 _validate_filters 确定性丢弃。
槽位形状的正确性见 test_planner_slots.py;此处只校验安全属性。
"""
from app.services import planner
from app.services.retrieval import build_where
from app.services.planner import _validate_filters

INJECTION = "St Lucia'; DROP TABLE courses; --"


def test_malicious_value_stays_in_params_never_in_sql():
    # 注入字符串作为 location 值:只能进 params,SQL 文本里只有占位符 %s
    sql, params = build_where({"location": INJECTION})
    assert sql == "location = %s"
    assert params == [INJECTION]
    assert INJECTION not in sql and ";" not in sql and "DROP" not in sql.upper()


def test_course_type_list_value_parameterized():
    # 列表值(NOT IN / IN)同样参数化:值进 params,SQL 只有 %s 数组占位
    sql, params = build_where({"course_type_exclude": ["thesis'; DROP", "research"]})
    assert sql == "course_type <> ALL(%s)"
    assert params == [["thesis'; DROP", "research"]]
    assert "DROP" not in sql.upper()


def test_validate_drops_hallucinated_columns():
    # LLM 脑补的列 / 自由 SQL 残留键一律丢弃,绝不进 build_where(取代 _clean_where 的整段清空)
    assert _validate_filters(
        {"requirement_type": "core", "title": "%ml%",
         "has_exam=false; drop table courses": 1}) == {}
    # 合法槽位保留、脑补键剔除(混入场景)
    assert _validate_filters({"has_exam": False, "requirement_type": "thesis"}) == \
        {"has_exam": False}


def test_validate_rejects_out_of_enum_level(monkeypatch):
    # level 按真实枚举校验,LLM 编的不存在层级值被丢弃(不会拼进 SQL)
    monkeypatch.setitem(planner._ENUM_CACHE, "level",
                        {"undergraduate", "postgraduate coursework"})
    assert _validate_filters({"level": "Master"}) == {}
    assert _validate_filters({"level": "Postgraduate Coursework"}) == \
        {"level": "Postgraduate Coursework"}


def test_build_where_only_emits_closed_set_columns():
    # 即便校验后的 dict 里塞进未知键(防御性),build_where 也只认 _WHERE_BUILDERS / course_type_*,
    # 未知键不产生任何 SQL 片段(列名闭集,结构性安全)
    sql, params = build_where({"has_exam": False, "evil_col": "x", "drop": 1})
    assert sql == "has_exam = %s" and params == [False]
