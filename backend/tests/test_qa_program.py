"""qa 专业题:带方向结构的专业走 simulator 引擎按方向枚举(覆盖 major 门控选修),
无方向结构的专业维持扁平枚举。只测确定性逻辑(不调 LLM,直接打 qa 的引擎辅助函数)。
依赖本地 DB(2559/5522 已入树)。运行:pytest tests/test_qa_program.py -q
"""
import psycopg
import pytest

from app.services import qa, program_lookup
from app.services.simulator import DSN


@pytest.fixture(scope="module")
def conn():
    with psycopg.connect(DSN) as c:
        c.read_only = True
        yield c


def test_engine_path_triggers_for_major_program(conn):
    ov = qa._structure_or_none(conn, "2559")
    assert ov and any(g["plan_name"] for g in ov["groups"]), "2559 应判为带方向结构 -> 走引擎"
    assert qa._structure_or_none(conn, "5522") is not None
    assert not any(g["plan_name"] for g in qa._structure_or_none(conn, "5522")["groups"]), \
        "5522 无方向 -> 保留扁平枚举"


def test_engine_p2c_elective_covers_major_gated(conn):
    ov = qa._structure_or_none(conn, "2559")
    courses, ans = qa._engine_p2c(conn, "Bachelor of Computer Science", "elective", ov)
    codes = {c["code"] for c in courses}
    assert len(codes) == len(courses), "卡片应去重"
    assert courses and all(c["requirement_type"] == "elective" for c in courses), \
        "选修查询的卡片应全部标 elective"
    # 比扁平直属选修多出 major 门控课
    flat = {r["course_code"] for r in program_lookup.courses_for_program(
        conn, "2559", "elective", direct_only=True)}
    assert codes - flat, "引擎枚举应覆盖扁平漏掉的 major 门控选修"
    # 文案按方向分组,且标注开放选修池
    assert "方向】" in ans and "可枚举" in ans


def test_engine_p2c_core_only_filters(conn):
    ov = qa._structure_or_none(conn, "2559")
    courses, _ = qa._engine_p2c(conn, "Bachelor of Computer Science", "core", ov)
    assert courses and all(c["requirement_type"] == "core" for c in courses), \
        "核心查询的卡片应全部标 core"


def test_ans_structured_empty_when_no_match(conn):
    ov = qa._structure_or_none(conn, "2559")
    # 构造一个无任何组命中的 req(用不存在的 kind 不可能,改测 titles 为空时的兜底)
    txt = qa._ans_p2c_structured("X", "elective", {"groups": []}, {})
    assert "未找到" in txt
