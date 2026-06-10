"""API 冒烟:状态/分支/搜索/排课/advise 护栏(LLM mock)。依赖本地 DB。"""
import pytest
from fastapi.testclient import TestClient

from app import main as server


@pytest.fixture(scope="module")
def client():
    return TestClient(server.app)


def test_state_default_branch(client):
    r = client.post("/api/sim/state", json={"program_id": "2559", "selected": ["CSSE1001"]})
    assert r.status_code == 200
    d = r.json()
    st = {e["ref"]: e for e in d["rules"]}
    assert st["C"]["inactive"] and st["C.1"]["inactive"]
    assert d["overall"]["branch"] == {"B|C": "B"}
    assert d["overall"]["total_counted"] == 2.0


def test_state_branch_switch_and_errors(client):
    d = client.post("/api/sim/state",
                    json={"program_id": "2559", "branch": ["C"]}).json()
    st = {e["ref"]: e for e in d["rules"]}
    assert st["B"]["inactive"] and not st["C.1"]["inactive"]
    assert client.post("/api/sim/state",
                       json={"program_id": "2559", "branch": ["Z"]}).status_code == 400
    assert client.post("/api/sim/state",
                       json={"program_id": "nope"}).status_code == 404


def test_out_of_tree_course_counts_into_f(client):
    d = client.post("/api/sim/state",
                    json={"program_id": "2559", "selected": ["PHIL1002"],
                          "placement": {"PHIL1002": 0}}).json()
    st = {e["ref"]: e for e in d["rules"]}
    assert st["F"]["units_counted"] == 2.0
    assert "PHIL1002" in d["selected_by_rule"]["F"]
    assert "PHIL1002" in d["courses"], "树外课也要 hydrate"


def test_course_search(client):
    rows = client.get("/api/sim/courses", params={"q": "philos"}).json()
    assert rows and all("PHIL" in x["code"] or "philos" in (x["title"] or "").lower()
                        for x in rows)
    rows2 = client.get("/api/sim/courses",
                       params={"q": "comp", "in_program": "2559"}).json()
    assert rows2 and len(rows2) <= 50
    assert client.get("/api/sim/courses", params={"q": "x"}).status_code == 400
    assert client.get("/api/sim/courses",
                      params={"q": "comp", "in_program": "nope"}).status_code == 404


def test_schedule_smoke(client):
    d = client.post("/api/sim/schedule",
                    json={"program_id": "2559",
                          "selected": ["CSSE1001", "CSSE2002", "COMP3506"]}).json()
    placed = {x["code"] for s in d["semesters"] for x in s["courses"]}
    unplaced = {u["code"] for u in d["unplaced"]}
    assert placed | unplaced == {"CSSE1001", "CSSE2002", "COMP3506"}


def test_advise_guardrail_strips_invented_codes(client, monkeypatch):
    from app.services import sim_advise
    monkeypatch.setattr(sim_advise.llm, "call",
                        lambda *a, **k: "1. ZZZZ9999 编造课 —— 应被剥除\n2. 真实建议见下")
    d = client.post("/api/sim/advise",
                    json={"program_id": "2559", "goal": "机器学习"}).json()
    assert d["candidates"], "应有候选"
    assert "ZZZZ9999" not in {c["code"] for c in d["candidates"]}
    assert "ZZZZ9999" not in (d["advice"] or "").splitlines()[0]
    assert "ZZZZ9999" in d["advice"], "被剥除的码要在警告行里显式列出"


def test_advise_empty_goal(client):
    assert client.post("/api/sim/advise",
                       json={"program_id": "2559", "goal": " "}).status_code == 400
