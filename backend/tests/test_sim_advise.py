"""sim_advise deterministic boundary: when the goal has a structured constraint (no exam / no group), the candidate pool
must be filtered by code against real data, never guessed by the LLM (red line 1 / rule 12). Only the deterministic pool is tested (generate=False, no LLM call).
Depends on the local DB (2559 already in the tree). Run: pytest tests/test_sim_advise.py -q
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
    """"No exam" is a structured fact: candidates must all be has_exam=False (regression: once recommended an exam course as no-exam)."""
    res = sim_advise.advise(conn, "2559", "我想选没考试的课", generate=False)
    assert res["candidates"], "应有候选"
    bad = [c["code"] for c in res["candidates"] if _has_exam(conn, c["code"]) is not False]
    assert not bad, f"含考试/未知的课不应进无考试候选:{bad}"


def test_topic_plus_no_exam_still_filters(conn):
    """Topic + structured constraint combined: after semantic recall, still filter deterministically by has_exam."""
    res = sim_advise.advise(conn, "2559", "我想做 AI 没考试的课", generate=False)
    bad = [c["code"] for c in res["candidates"] if _has_exam(conn, c["code"]) is not False]
    assert not bad, f"含考试的课不应进候选:{bad}"


def test_pure_topic_goal_not_over_filtered(conn):
    """A pure topic goal (no structured constraint) should not be forced through a has_exam filter — otherwise it wrongly drops valid candidates."""
    res = sim_advise.advise(conn, "2559", "我想做 AI 和机器学习", generate=False)
    assert res["candidates"], "纯主题目标应能召回候选"
    # Do not force all to be exam-free (not required); at least prove the filter did not drop all exam topic courses
    assert any(_has_exam(conn, c["code"]) is True for c in res["candidates"]), \
        "纯主题召回里应允许有考试的课(未施加结构化约束)"
