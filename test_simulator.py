"""引擎回归:min–max 收敛 / 公式 / 分支 / SubRule / 归属 / claims / level cap。
依赖本地 DB(2559 已入新树)。运行:pytest test_simulator.py -q
"""
import psycopg
import pytest

import simulator
from simulator import PlanSimulator, parse_rule_logic, _logic_refs


@pytest.fixture(scope="module")
def conn():
    with psycopg.connect(simulator.DSN) as c:
        c.read_only = True
        yield c


@pytest.fixture()
def sim(conn):
    return PlanSimulator(conn, "2559")


# ---------- 公式解析 ----------
def test_parse_rule_logic_tree():
    t = parse_rule_logic("Part A AND ( Part B OR Part C ) AND Part D")
    assert t["op"] == "and" and len(t["children"]) == 3
    assert t["children"][1] == {"op": "or", "children": [
        {"op": "part", "ref": "B"}, {"op": "part", "ref": "C"}]}
    assert _logic_refs(t) == {"A", "B", "C", "D"}


def test_parse_rule_logic_lowercase_and_leading_junk():
    assert parse_rule_logic("Part A and Part B")["op"] == "and"
    assert parse_rule_logic("AND Part A AND Part B")["op"] == "and"


def test_parse_rule_logic_rejects_garbage():
    assert parse_rule_logic("Part A AND 8 units of MATH") is None
    assert parse_rule_logic("Part A AND ( Part B") is None
    assert parse_rule_logic("") is None


# ---------- 收敛(B1/B2) ----------
def test_rule_open_until_max(sim):
    c2 = next(r for r in sim.rules if r["ref"] == "C.2")
    codes = [it["code"] for it in c2["items"] if it.get("kind") == "course"]
    sim.choose_branch("C")
    sim.select(codes[0])
    sim.select(codes[1])                      # 4u = min,旧逻辑会在这里收敛
    assert "C.2" in sim.available_by_rule(), "满 min 不应收敛"
    for c in codes[2:11]:
        sim.select(c)                         # 22u = max
    assert "C.2" not in sim.available_by_rule(), "到 max 应收敛"


def test_d_rule_visible_initially(sim):
    br = sim.available_by_rule()
    assert "D" in br and len(br["D"]) > 0


def test_flatten_equals_available(sim):
    sim.select("CSSE1001")
    sim.choose_plan("ARTINC2559")
    flat = []
    for slots in sim.available_by_rule().values():
        for s in slots:
            flat += [s["code"]] if s["kind"] == "course" else s["options"]
    assert sorted(set(flat)) == sorted(set(sim.available()))


# ---------- 分支(Either/Or) ----------
def test_default_branch_is_major(sim):
    assert sim.branch_state() == {"B|C": "B"}
    inact = sim._inactive_refs()
    assert inact == {"C", "C.1", "C.2"}


def test_choose_branch_switches(sim):
    sim.choose_branch("C")
    assert sim._inactive_refs() == {"B"}
    br = sim.available_by_rule()
    assert "C.1" in br and "B" not in br


def test_choose_branch_unknown_raises(sim):
    with pytest.raises(ValueError):
        sim.choose_branch("Z")


def test_inactive_rule_counts_zero(sim):
    c1 = next(r for r in sim.rules if r["ref"] == "C.1")
    code = next(it["code"] for it in c1["items"] if it.get("kind") == "course")
    sim.select(code)                          # 默认 Major 路径,C.1 失活
    st = {e["ref"]: e for e in sim.status()}
    assert st["C.1"]["units_counted"] == 0.0 and st["C.1"]["inactive"]
    assert sim.attribution()["assigned"][code] == "E"   # 流入 E(程序课表)


# ---------- SubRule 父规则(C 8–24) ----------
def test_subrule_parent_sums_and_caps(sim):
    sim.choose_branch("C")
    c1 = [it["code"] for r in sim.rules if r["ref"] == "C.1"
          for it in r["items"] if it.get("kind") == "course"]
    c2 = [it["code"] for r in sim.rules if r["ref"] == "C.2"
          for it in r["items"] if it.get("kind") == "course"]
    for c in c1[:5] + c2[:11]:                # 10 + 22 = 32 raw
        sim.select(c)
    st = {e["ref"]: e for e in sim.status()}
    assert st["C"]["units_done"] == 32.0
    assert st["C"]["units_counted"] == 24.0 and st["C"]["over_max"]
    assert st["C"]["child_of"] is None
    assert st["C.1"]["child_of"] == "C" and st["C.2"]["child_of"] == "C"


def test_subrule_done_needs_children_formula(sim):
    sim.choose_branch("C")
    c2 = [it["code"] for r in sim.rules if r["ref"] == "C.2"
          for it in r["items"] if it.get("kind") == "course"]
    for c in c2[:4]:                          # C.2 8u(超 C 的 min=8),但 C.1 没满 min
        sim.select(c)
    st = {e["ref"]: e for e in sim.status()}
    assert st["C"]["units_counted"] == 8.0
    assert not st["C"]["done"], "C.1 未满 min,C 的子公式不满足"


# ---------- 归属(D 枚举 → E 程序课表 → F 任意) ----------
def test_attribution_priority_and_unattributed(sim):
    outside = sorted(sim._all_codes - sim._all_referenced_codes())[0]
    sim.select(outside)
    att = sim.attribution()
    assert att["assigned"][outside] == "F"
    sim.select("NOPE9999")                    # 不在 courses 库
    att = sim.attribution()
    assert "NOPE9999" in att["unattributed"]


def test_attribution_respects_level_cap_for_f(sim):
    pg_code = next(c for c in sorted(sim._all_codes - sim._all_referenced_codes())
                   if c[4] == "7")            # 7000 级树外课
    sim.select(pg_code)
    att = sim.attribution()
    assert pg_code in att["unattributed"], "F 限 undergraduate(level<=6)"


def test_overall_total_no_double_count(sim, conn):
    sim.choose_plan("ARTINC2559")
    for c in ("COMP2701", "COMP3702", "COMP4702", "DECO2801", "MATH1051",
              "COMP3710", "COMP4703", "STAT3006"):
        sim.select(c)                         # DECO2801 同时在 D 枚举表 -> 只计一次
    st = {e["ref"]: e for e in sim.status()}
    assert st["B"]["units_counted"] == 16.0
    assert st["D"]["units_counted"] == 0.0
    assert sim.overall()["total_counted"] == 16.0


# ---------- exclude / equivalence / level cap ----------
def test_program_exclude(sim):
    assert "MATH1040" in sim.excluded
    assert "MATH1040" not in sim.available()


def test_equivalence_converges(sim):
    assert "MATH1081" in sim.available()
    sim.select("MATH1061")
    assert "MATH1081" not in sim.available()


def test_dual_level_caps(sim):
    d = next(r for r in sim.rules if r["ref"] == "D")
    l1 = [it["code"] for it in d["items"]
          if it.get("kind") == "course" and it["code"][4] == "1"]
    for c in l1[:2]:
        sim.select(c)
    caps = {(c["scope"], c["level"]): c for c in sim.level_cap_status()}
    assert caps[("program", 1)]["max_units"] == 24
    assert caps[("electives", 1)]["max_units"] == 14
    assert caps[("electives", 1)]["used"] == 4.0


def test_formula_satisfied_both_paths(conn):
    # Major 路径
    s = PlanSimulator(conn, "2559")
    a = next(r for r in s.rules if r["ref"] == "A")
    for it in a["items"]:
        if it["kind"] == "course":
            s.select(it["code"])
        elif it["kind"] == "equivalence":
            s.select(it["options"][0]["code"])
    s.choose_plan("ARTINC2559")
    for c in ("COMP2701", "COMP3702", "COMP4702", "DECO2801", "MATH1051",
              "COMP3710", "COMP4703", "STAT3006"):
        s.select(c)
    d = next(r for r in s.rules if r["ref"] == "D")
    for c in [it["code"] for it in d["items"]
              if it.get("kind") == "course" and it["code"] not in s.selected][:4]:
        s.select(c)
    ov = s.overall()
    assert ov["total_counted"] == 48.0 and ov["formula_satisfied"]
    # No-Major 路径
    s2 = PlanSimulator(conn, "2559")
    s2.choose_branch("C")
    for r in ("C.1", "C.2"):
        rule = next(x for x in s2.rules if x["ref"] == r)
        for it in rule["items"][:6]:
            if it.get("kind") == "course":
                s2.select(it["code"])
    for it in next(x for x in s2.rules if x["ref"] == "A")["items"]:
        if it["kind"] == "course":
            s2.select(it["code"])
        elif it["kind"] == "equivalence":
            s2.select(it["options"][0]["code"])
    assert s2.overall()["formula_satisfied"]
