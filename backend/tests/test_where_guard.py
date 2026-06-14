"""WHERE 双层防护回归(纯函数,无 DB):planner._clean_where + retrieval.guard_where。

覆盖原始 bug(LLM 脑补 requirement_type + NOT IN 直接 500)与新增 course_type / NOT IN 能力,
以及评审发现的边界:NOT IN 仅限 course_type、两种否定写法、非 ASCII 绕过、IS 两层一致。
"""
import pytest

from app.services.planner import _clean_where
from app.services.retrieval import guard_where

# 触发原始报错的 where:非白名单列 + NOT IN
BUGGY = "has_exam=false AND requirement_type NOT IN ('placement', 'thesis', 'research')"
# 修复后 planner 应产出的合法 where
FIXED = "has_exam=false AND course_type NOT IN ('placement','thesis','research')"
# NBSP 夹在 token 之间:\s 会放过,但剥离字面量后非 ASCII 必须被拦
NBSP_WHERE = "has_exam=false AND course_type='thesis'"


def test_clean_where_clears_hallucinated_column():
    # _clean_where 必须确定性清空(整段),不放行给下游 guard
    assert _clean_where(BUGGY) == ""
    assert _clean_where("requirement_type='core'") == ""
    assert _clean_where("foo IS NOT NULL") == ""
    # IS guard 不支持,_clean_where 同步清掉(两层语法一致)
    assert _clean_where("course_type IS NULL") == ""


def test_clean_where_keeps_valid_where():
    assert _clean_where(FIXED) == FIXED
    assert _clean_where("has_exam=false") == "has_exam=false"
    assert _clean_where("course_type='thesis'") == "course_type='thesis'"


def test_guard_where_rejects_buggy_and_injection():
    for bad in [
        BUGGY,
        "requirement_type NOT IN ('thesis')",
        "code IN (SELECT code FROM programs)",
        "course_type NOT IN (select 1)",
        "has_exam=false; drop table courses",
        # NOT IN 仅对 course_type 开放:可空列禁用(否则静默漏掉 NULL 行)
        "location NOT IN ('Gatton')",
        "NOT location IN ('Gatton')",
        NBSP_WHERE,
    ]:
        with pytest.raises(ValueError):
            guard_where(bad)


def test_guard_where_allows_course_type_in_and_not_in():
    for good in [
        FIXED,
        "course_type='thesis'",
        "course_type IN ('research','thesis')",
        "course_type NOT IN ('placement')",
        "NOT course_type IN ('placement','thesis')",
        "has_exam=false AND NOT course_type IN ('placement')",
    ]:
        assert guard_where(good) == good
