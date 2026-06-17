"""槽位化重构步骤2回归(纯函数,无 DB):build_where 拼装 + _validate_filters 校验。

build_where 用与旧 guard_where 自测用例(retrieval __main__)等价的逻辑 WHERE 对照:
同样的条件,现在产出参数化 (sql_with_%s, params),注入安全是结构性的。
_validate_filters 对照旧 _clean_where:未知键/非法值丢弃,location 原值保留(Gatton 红线)。
"""
from app.services import planner
from app.services.retrieval import build_where, describe_where
from app.services.planner import _validate_filters, _as_number


# ---------- build_where:与旧 guard_where 必过用例的等价对照 ----------

def test_build_where_single_bool():
    # 旧 "has_exam=false"
    assert build_where({"has_exam": False}) == ("has_exam = %s", [False])
    assert build_where({"has_exam": True}) == ("has_exam = %s", [True])


def test_build_where_level_and_units_order_stable():
    # 旧 "level='Postgraduate Coursework' AND units=2";顺序由 _WHERE_BUILDERS 固定(level 在 units 前)
    assert build_where({"level": "Postgraduate Coursework", "units": 2}) == (
        "level = %s AND units = %s", ["Postgraduate Coursework", 2])
    # 入参 dict 顺序不影响输出顺序(确定性)
    assert build_where({"units": 2, "level": "Postgraduate Coursework"}) == (
        "level = %s AND units = %s", ["Postgraduate Coursework", 2])


def test_build_where_location_literal():
    # 旧 "location='St Lucia'";非枚举值(Gatton)同样原样进 params -> 命中 0,绝不被换值
    assert build_where({"location": "St Lucia"}) == ("location = %s", ["St Lucia"])
    assert build_where({"location": "Gatton"}) == ("location = %s", ["Gatton"])


def test_build_where_course_type_exclude_is_not_in_equivalent():
    # 旧 "has_exam=false AND course_type NOT IN ('placement','thesis','research')"
    # <> ALL(数组) ≡ NOT IN(course_type NOT NULL,无三值逻辑漏排)
    sql, params = build_where(
        {"has_exam": False, "course_type_exclude": ["placement", "thesis", "research"]})
    assert sql == "has_exam = %s AND course_type <> ALL(%s)"
    assert params == [False, ["placement", "thesis", "research"]]


def test_build_where_course_type_only_is_in_equivalent():
    # 旧 "course_type='thesis'"(= 或 IN 统一成 = ANY 数组)
    assert build_where({"course_type_only": ["thesis"]}) == (
        "course_type = ANY(%s)", [["thesis"]])
    assert build_where({"course_type_only": ["coursework", "placement"]}) == (
        "course_type = ANY(%s)", [["coursework", "placement"]])


def test_build_where_all_dimensions_order():
    # 全维度一起:顺序严格按 _WHERE_BUILDERS,course_type_only 在 exclude 前,均在尾部
    sql, params = build_where({
        "has_exam": True, "has_hurdle": False, "midterm_status": "has",
        "group_status": "none", "level": "Undergraduate", "units": 2,
        "location": "St Lucia", "attendance_mode": "In Person", "semester": "S1",
        "course_type_only": ["coursework"], "course_type_exclude": ["thesis"]})
    assert sql == (
        "has_exam = %s AND has_hurdle = %s AND midterm_status = %s AND "
        "group_status = %s AND level = %s AND units = %s AND location = %s AND "
        "attendance_mode = %s AND semester = %s AND course_type = ANY(%s) AND "
        "course_type <> ALL(%s)")
    assert params == [True, False, "has", "none", "Undergraduate", 2, "St Lucia",
                      "In Person", "S1", ["coursework"], ["thesis"]]


def test_build_where_empty_is_pure_no_raise():
    # 纯函数:空 filters / None 返回 ("", []),不抛(是否容忍空由调用方决定)
    assert build_where({}) == ("", [])
    assert build_where(None) == ("", [])
    # 全 None / 空列表的维度等于「不过滤」,不出现在片段里
    assert build_where({"has_exam": None, "level": None,
                        "course_type_exclude": [], "course_type_only": []}) == ("", [])


def test_build_where_false_and_zero_not_dropped():
    # is None 判定:False / 0 是有效值,绝不能被当缺省丢掉
    assert build_where({"has_exam": False}) == ("has_exam = %s", [False])
    assert build_where({"units": 0}) == ("units = %s", [0])


def test_build_where_code_level_substring_first_digit():
    # code 首位数字筛年级:取 code 第一个数字字符(等价 _first_digit),值进 params
    assert build_where({"code_level": ["1", "3"]}) == (
        "substring(code from '[0-9]') = ANY(%s)", [["1", "3"]])
    # 与其它维度组合:code_level 片段在尾部(course_type 之后)
    sql, params = build_where({"has_exam": False, "code_level": ["1"]})
    assert sql == "has_exam = %s AND substring(code from '[0-9]') = ANY(%s)"
    assert params == [False, ["1"]]
    # 空列表 = 不过滤
    assert build_where({"code_level": []}) == ("", [])


# ---------- _validate_filters:对照旧 _clean_where 的净化语义 ----------

def test_validate_drops_unknown_keys():
    # 未知键(LLM 脑补的列/自由 SQL 残留)一律丢弃,不进 SQL
    out = _validate_filters({"has_exam": False, "title": "%ml%", "drop table": 1,
                             "requirement_type": "thesis"})
    assert out == {"has_exam": False}


def test_validate_bool_type_enforced():
    # bool 槽位须真布尔;字符串 "false"/1 等非布尔丢弃(规则19:不静默 coerce)
    assert _validate_filters({"has_exam": False, "has_hurdle": True}) == \
        {"has_exam": False, "has_hurdle": True}
    assert _validate_filters({"has_exam": "false"}) == {}
    assert _validate_filters({"has_exam": 1}) == {}
    assert _validate_filters({"has_exam": None}) == {}


def test_validate_tristate():
    assert _validate_filters({"midterm_status": "none", "group_status": "has"}) == \
        {"midterm_status": "none", "group_status": "has"}
    # 大小写归一,unknown 合法
    assert _validate_filters({"midterm_status": "NONE"}) == {"midterm_status": "none"}
    assert _validate_filters({"group_status": "unknown"}) == {"group_status": "unknown"}
    # 非法三态值丢弃
    assert _validate_filters({"midterm_status": "maybe"}) == {}


def test_validate_semester_enum():
    assert _validate_filters({"semester": "S1"}) == {"semester": "S1"}
    assert _validate_filters({"semester": "S2"}) == {"semester": "S2"}
    # 非 S1/S2 丢弃(语义上「都」由 both_semesters 路径处理,不进单 semester 槽)
    assert _validate_filters({"semester": "S3"}) == {}


def test_validate_level_against_real_enum(monkeypatch):
    # level 按真实 DB 枚举校验(同 _validate_coord_unit 模式):不在枚举内丢弃 + 记日志
    monkeypatch.setitem(planner._ENUM_CACHE, "level",
                        {"undergraduate", "postgraduate coursework"})
    assert _validate_filters({"level": "Postgraduate Coursework"}) == \
        {"level": "Postgraduate Coursework"}
    assert _validate_filters({"level": "undergraduate"}) == {"level": "undergraduate"}
    # LLM 脑补的不存在层级值(Master/PG)丢弃,绝不进 SQL
    assert _validate_filters({"level": "Master"}) == {}
    assert _validate_filters({"level": "PG"}) == {}


def test_validate_units_numeric():
    assert _validate_filters({"units": 2}) == {"units": 2}
    assert _validate_filters({"units": 2.0}) == {"units": 2}      # 整数值收敛成 int
    assert _validate_filters({"units": "2"}) == {"units": 2}      # 数字字符串转数值
    assert _validate_filters({"units": "abc"}) == {}              # 非数值丢弃
    assert _validate_filters({"units": True}) == {}              # bool 不算数值


def test_validate_location_literal_kept_even_if_non_enum():
    # 红线:location/attendance_mode 照搬用户原值,不校验枚举(Gatton/Online 故意制造空集)
    assert _validate_filters({"location": "Gatton"}) == {"location": "Gatton"}
    assert _validate_filters({"location": "St Lucia"}) == {"location": "St Lucia"}
    assert _validate_filters({"attendance_mode": "Online"}) == {"attendance_mode": "Online"}
    # 空串/非字符串丢弃
    assert _validate_filters({"location": ""}) == {}
    assert _validate_filters({"location": 123}) == {}


def test_validate_course_type_lists_filtered_to_closed_set():
    # 课型列表过滤到合法闭集(去重升序),非法值丢弃 + 记日志
    assert _validate_filters({"course_type_exclude": ["thesis", "research", "placement"]}) == \
        {"course_type_exclude": ["placement", "research", "thesis"]}
    assert _validate_filters({"course_type_only": ["coursework"]}) == \
        {"course_type_only": ["coursework"]}
    # 含非法类型:只留合法的;全非法 -> 该键不出现
    assert _validate_filters({"course_type_exclude": ["thesis", "bogus"]}) == \
        {"course_type_exclude": ["thesis"]}
    assert _validate_filters({"course_type_exclude": ["bogus"]}) == {}
    # 非列表丢弃
    assert _validate_filters({"course_type_exclude": "thesis"}) == {}


def test_validate_code_level_digit_list():
    # code_level 是数字列表(确定性注入,值须单字符 1-9,去重升序)
    assert _validate_filters({"code_level": ["3", "1", "1"]}) == {"code_level": ["1", "3"]}
    # 非法元素(多位/非数字/0)丢弃;全非法 -> 该键不出现
    assert _validate_filters({"code_level": ["1", "12", "x", "0"]}) == {"code_level": ["1"]}
    assert _validate_filters({"code_level": ["x"]}) == {}
    # 非列表丢弃
    assert _validate_filters({"code_level": "1"}) == {}


def test_describe_where_renders_code_level():
    # describe_where(build_where 可读对偶):code_level 渲染成可读年级集合
    assert describe_where({"code_level": ["1", "3"]}) == "code首位∈{1,3}"
    assert describe_where({"has_exam": False, "code_level": ["1"]}) == \
        "has_exam=false AND code首位∈{1}"


def test_validate_none_and_non_dict_safe():
    assert _validate_filters(None) == {}
    assert _validate_filters("not a dict") == {}
    assert _validate_filters({}) == {}


def test_as_number_helper():
    assert _as_number(2) == 2
    assert _as_number(2.5) == 2.5
    assert _as_number("2") == 2
    assert _as_number("2.5") == 2.5
    assert _as_number("x") is None
    assert _as_number(True) is None
    assert _as_number(None) is None
