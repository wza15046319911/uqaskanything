"""sim_advise 确定性边界:目标含结构化约束(没考试/没小组)时,候选池必须由代码按真实
数据过滤,绝不靠 LLM 猜(红线1/规则12)。只测确定性定池(generate=False,不调 LLM)。
依赖本地 DB(2559 已入树)。运行:pytest tests/test_sim_advise.py -q
"""
import psycopg
import pytest

from app.services import sim_advise
from app.services.simulator import DSN


@pytest.fixture(scope="module")
def conn():
    with psycopg.connect(DSN) as c:
        c.read_only = True
        yield c


def _has_exam(conn, code):
    r = conn.execute("SELECT has_exam FROM courses WHERE code=%s LIMIT 1", (code,)).fetchone()
    return r[0] if r else None


def test_no_exam_goal_only_no_exam_candidates(conn):
    """「没考试」是结构化事实:候选必须全是 has_exam=False(回归:曾把有考试的课推为没考试)。"""
    res = sim_advise.advise(conn, "2559", "我想选没考试的课", generate=False)
    assert res["candidates"], "应有候选"
    bad = [c["code"] for c in res["candidates"] if _has_exam(conn, c["code"]) is not False]
    assert not bad, f"含考试/未知的课不应进无考试候选:{bad}"


def test_topic_plus_no_exam_still_filters(conn):
    """主题 + 结构化约束组合:语义召回后仍按 has_exam 确定性过滤。"""
    res = sim_advise.advise(conn, "2559", "我想做 AI 没考试的课", generate=False)
    bad = [c["code"] for c in res["candidates"] if _has_exam(conn, c["code"]) is not False]
    assert not bad, f"含考试的课不应进候选:{bad}"


def test_pure_topic_goal_not_over_filtered(conn):
    """纯主题目标(无结构化约束)不应被强加 has_exam 过滤——否则会误删合法候选。"""
    res = sim_advise.advise(conn, "2559", "我想做 AI 和机器学习", generate=False)
    assert res["candidates"], "纯主题目标应能召回候选"
    # 不强制全部无考试(没要求);至少证明过滤没把有考试的主题课全删掉
    assert any(_has_exam(conn, c["code"]) is True for c in res["candidates"]), \
        "纯主题召回里应允许有考试的课(未施加结构化约束)"
