"""先修解析回归:AND/OR 树 / 括号 / 缩写展开 / raw 兜底 / 空。无 DB 依赖。"""
from scraper import parse_prereq


def test_or_tree():
    t = parse_prereq("CSSE1001 or ENGG1001")
    assert t == {"op": "or", "children": [
        {"op": "course", "code": "CSSE1001"}, {"op": "course", "code": "ENGG1001"}]}


def test_plus_is_and():
    t = parse_prereq("CSSE1001 + MATH1061")
    assert t["op"] == "and"
    assert [c["code"] for c in t["children"]] == ["CSSE1001", "MATH1061"]


def test_parens_precedence():
    t = parse_prereq("(CSSE1001 or ENGG1001) and MATH1061")
    assert t["op"] == "and"
    assert t["children"][0]["op"] == "or"
    assert t["children"][1] == {"op": "course", "code": "MATH1061"}


def test_abbrev_expansion():
    t = parse_prereq("ACCT1110 or 1111")
    assert [c["code"] for c in t["children"]] == ["ACCT1110", "ACCT1111"]


def test_unparseable_falls_back_to_raw():
    t = parse_prereq("Permission of Head of School")
    assert t == {"op": "raw", "unparsed": "Permission of Head of School"}


def test_mixed_unknown_token_is_raw_not_partial():
    raw = "CSSE1001 and 8 units of MATH courses"
    t = parse_prereq(raw)
    assert t["op"] == "raw" and t["unparsed"] == raw


def test_empty_is_none():
    assert parse_prereq("") is None
    assert parse_prereq("   ") is None


def test_satisfied_soft_gate():
    from simulator import satisfied
    ok, why = satisfied(None, set())
    assert ok and why is None                       # 无先修 = 解锁
    ok, why = satisfied({"op": "raw", "unparsed": "x"}, set())
    assert ok and "无法解析" in why                  # raw = 软警告不硬挡
    tree = parse_prereq("CSSE1001 or ENGG1001")
    assert satisfied(tree, {"ENGG1001"})[0]
    ok, why = satisfied(tree, set())
    assert not ok and "CSSE1001" in why
