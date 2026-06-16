"""
program_lookup.py — program 维度问答助手(纯 SQL)
查 program_course / programs 两张表,回答「某课归属哪些 program」「某 program 有哪些课」「按名字找 program」。
全部只读 SELECT,psycopg3 风格;可 import 的函数,文件末尾用真实 DB 自测。

公开函数:
    programs_for_course(conn, code) -> list[dict]
    courses_for_program(conn, program_id, requirement_type=None, direct_only=True) -> list[dict]
    find_program(conn, name_substr) -> list[tuple[str, str]]
"""
from __future__ import annotations
import os
import re

import psycopg

from app.core.config import DSN


def programs_for_course(conn, code: str, collapse: bool = False) -> list[dict]:
    """某课是哪些 program 的必修/选修。join programs 取 title。

    collapse=False(默认,旧行为):按 requirement_type, title 排序,每个 program 可能
        因多个 via_plan/course_list 而出现多行(放大行)。
    collapse=True:按 (program_id, requirement_type) 折叠去重,把 course_list/via_plan/
        plan_subtype 聚合成列表(各自去重),并给出原始行数 row_count;排序以 program_id
        优先,保证同一 program 的多个 requirement_type 连续相邻。
    """
    if not collapse:
        sql = """
            SELECT pc.program_id, p.title, pc.requirement_type,
                   pc.course_list, pc.via_plan, pc.plan_subtype, pc.equiv_group
            FROM program_course pc
            LEFT JOIN programs p ON p.program_id = pc.program_id
            WHERE pc.course_code = %s
            ORDER BY pc.requirement_type, p.title
        """
        rows = conn.execute(sql, (code,)).fetchall()
        return [
            {"program_id": r[0], "title": r[1], "requirement_type": r[2],
             "course_list": r[3], "via_plan": r[4], "plan_subtype": r[5],
             "equiv_group": r[6]}
            for r in rows
        ]

    # collapse=True:在 SQL 里按 (program_id, requirement_type) 聚合,
    # 用 array_agg(DISTINCT ...) 把多行的 course_list/via_plan/plan_subtype 收成列表。
    sql = """
        SELECT pc.program_id, p.title, pc.requirement_type,
               array_agg(DISTINCT pc.course_list) AS course_lists,
               array_agg(DISTINCT pc.via_plan) AS via_plans,
               array_agg(DISTINCT pc.plan_subtype) AS plan_subtypes,
               count(*) AS row_count
        FROM program_course pc
        LEFT JOIN programs p ON p.program_id = pc.program_id
        WHERE pc.course_code = %s
        GROUP BY pc.program_id, p.title, pc.requirement_type
        ORDER BY pc.program_id, pc.requirement_type
    """
    rows = conn.execute(sql, (code,)).fetchall()
    return [
        {"program_id": r[0], "title": r[1], "requirement_type": r[2],
         "course_lists": r[3], "via_plans": r[4], "plan_subtypes": r[5],
         "row_count": r[6]}
        for r in rows
    ]


def courses_for_program(conn, program_id: str, requirement_type: str | None = None,
                        direct_only: bool = True) -> list[dict]:
    """某 program 的课;direct_only 时只取 via_plan='';left join courses 补 title;按 course_code 排序。

    注意:courses.code 不是主键(一门课有多个 offering),join 可能放大行数,
    取 DISTINCT 的 (program_id, course_code, requirement_type, course_list, via_plan, plan_subtype)
    再补 title,避免重复行。

    假设:DISTINCT ON 把同一 course_code 的多个 offering 折成一行,这隐含「同 code 同 title」。
    当前库内每个 code 的 title 唯一,该假设成立。若将来同 code 出现多 title,
    ORDER BY 末尾的 c.title 升序会保留字母序最小的那行(确定性,不会随机丢数据)。
    """
    conds = ["pc.program_id = %s"]
    params: list = [program_id]
    if direct_only:
        conds.append("pc.via_plan = ''")
    if requirement_type is not None:
        conds.append("pc.requirement_type = %s")
        params.append(requirement_type)
    where = " AND ".join(conds)
    sql = f"""
        SELECT DISTINCT ON (pc.course_code, pc.requirement_type, pc.course_list,
                            pc.via_plan, pc.plan_subtype, pc.equiv_group)
               pc.course_code, c.title, pc.requirement_type,
               pc.course_list, pc.via_plan, pc.plan_subtype, pc.equiv_group
        FROM program_course pc
        LEFT JOIN courses c ON c.code = pc.course_code
        WHERE {where}
        ORDER BY pc.course_code, pc.requirement_type, pc.course_list,
                 pc.via_plan, pc.plan_subtype, pc.equiv_group, c.title
    """
    rows = conn.execute(sql, tuple(params)).fetchall()
    return [
        {"course_code": r[0], "title": r[1], "requirement_type": r[2],
         "course_list": r[3], "via_plan": r[4], "plan_subtype": r[5],
         "equiv_group": r[6]}
        for r in rows
    ]


def excluded_courses(conn, program_id: str) -> list[str]:
    """该 program 无条件禁修的课码(program_exclude 表,如 BCompSc 禁 MATH1040)。"""
    rows = conn.execute(
        "SELECT course_code FROM program_exclude WHERE program_id = %s ORDER BY course_code",
        (program_id,)).fetchall()
    return [r[0] for r in rows]


def is_excluded(conn, program_id: str, course_code: str) -> bool:
    """该 program 是否无条件禁修某课。"""
    return conn.execute(
        "SELECT 1 FROM program_exclude WHERE program_id = %s AND course_code = %s LIMIT 1",
        (program_id, course_code.upper())).fetchone() is not None


def programs_excluding(conn, course_code: str) -> list[tuple[str, str]]:
    """禁修某课的所有 program,返回 [(program_id, title)]。"""
    rows = conn.execute(
        "SELECT pe.program_id, p.title FROM program_exclude pe "
        "LEFT JOIN programs p ON p.program_id = pe.program_id "
        "WHERE pe.course_code = %s ORDER BY p.title", (course_code.upper(),)).fetchall()
    return [(r[0], r[1]) for r in rows]


def aux_rules(conn, program_id: str) -> list[dict]:
    """该 program 的全部附加规则(禁课/level 上限/plan 冲突…),供展示。"""
    row = conn.execute("SELECT aux_rules FROM programs WHERE program_id = %s", (program_id,)).fetchone()
    return row[0] if row and row[0] else []


def has_plan_level_core(conn, program_id: str) -> bool:
    """该 program 是否有 plan 层(via_plan<>'')核心课。direct_only 的 p2c 查询不会展示这些
    (major/方向的核心课),用于在答案里补一句提示。"""
    row = conn.execute(
        "SELECT 1 FROM program_course WHERE program_id = %s "
        "AND requirement_type = 'core' AND via_plan <> '' LIMIT 1", (program_id,)).fetchone()
    return row is not None


def find_program(conn, name_substr: str) -> list[tuple[str, str]]:
    """按 title ILIKE 模糊找 program,返回 [(program_id, title)]。

    排序确定性:精确同名 > 标题以该名开头 > 标题更短(更具体)> 字母序。
    调用方取 progs[0],所以子串查询(如 'Master of Data Science')必须让独立专业
    排在组合专业('Bachelor of X / Master of Data Science')之前,否则答非所问。

    空串/纯空白短路返回 []:否则 '%%' 会命中全部 program(无意义的全表返回)。
    """
    if name_substr is None or not name_substr.strip():
        return []
    # 归一内部空白:用户/LLM 可能在词间多打空格(如 'Bachelor of  Information Technology'),
    # 而库内 title 是单空格,ILIKE 对空格敏感会整串落空。折成单空格再匹配。
    name = re.sub(r"\s+", " ", name_substr.strip())
    sql = """
        SELECT program_id, title
        FROM programs
        WHERE title ILIKE %s
        ORDER BY (LOWER(title) = LOWER(%s)) DESC,
                 (title ILIKE %s) DESC,
                 length(title) ASC,
                 title ASC
    """
    rows = conn.execute(sql, (f"%{name}%", name, f"{name}%")).fetchall()
    return [(r[0], r[1]) for r in rows]


if __name__ == "__main__":
    with psycopg.connect(DSN) as conn:
        conn.read_only = True

        print("=== find_program('Computer Science') ===")
        progs = find_program(conn, "Computer Science")
        print(f"命中 {len(progs)} 个:")
        for pid, title in progs:
            print(f"  {pid}  {title}")

        print("\n=== programs_for_course('CSSE1001') ===")
        owners = programs_for_course(conn, "CSSE1001")
        print(f"命中 {len(owners)} 行,前 5:")
        for d in owners[:5]:
            print(f"  {d['program_id']}  [{d['requirement_type']}]  {d['title']}  "
                  f"(list={d['course_list']!r}, via_plan={d['via_plan']!r}, "
                  f"plan_subtype={d['plan_subtype']!r})")

        print("\n=== courses_for_program('2559', 'core') ===")
        cs = courses_for_program(conn, "2559", "core")
        print(f"命中 {len(cs)} 行:")
        for d in cs:
            print(f"  {d['course_code']}  {d['title']}  "
                  f"(list={d['course_list']!r}, via_plan={d['via_plan']!r})")

        # ---- 复现用例验证(断言失败即报错,不静默) ----
        print("\n=== 复现用例验证 ===")
        assert find_program(conn, "") == [], "find_program('') 应短路返回 []"
        assert find_program(conn, "  ") == [], "find_program('  ') 应短路返回 []"
        assert len(find_program(conn, "Computer Science")) > 0, "Computer Science 仍应命中"
        # 子串查询命中多个时,独立专业(精确同名)必须排第一,否则 progs[0] 选到组合专业
        mds = find_program(conn, "Master of Data Science")
        assert len(mds) > 1, "Master of Data Science 应命中独立 + 组合多个专业"
        assert mds[0][1].lower() == "master of data science", \
            f"独立专业应排第一,实际 progs[0]={mds[0]!r}"
        print(f"[OK] find_program 空串/纯空白短路;'Computer Science' 仍命中;"
              f"'Master of Data Science' 在 {len(mds)} 个命中中独立专业排第一({mds[0][0]})")

        raw = programs_for_course(conn, "CSSE1001")
        collapsed = programs_for_course(conn, "CSSE1001", collapse=True)
        distinct_pids = len({d["program_id"] for d in raw})
        assert len(collapsed) >= distinct_pids, "折叠后行数不应少于 distinct program_id"
        # 折叠行数 = distinct (program_id, requirement_type),且 distinct program_id 数一致
        assert len({d["program_id"] for d in collapsed}) == distinct_pids, \
            "折叠前后 distinct program_id 数应一致"
        # 同一 program_id 的行必须连续(排序 program_id 优先)
        seen: set[str] = set()
        prev = None
        for d in collapsed:
            pid = d["program_id"]
            if pid != prev:
                assert pid not in seen, f"program_id {pid} 不连续"
                seen.add(pid)
                prev = pid
        print(f"[OK] CSSE1001 放大 {len(raw)} 行 -> distinct program_id {distinct_pids} 个;"
              f"collapse 折成 {len(collapsed)} 行(按 program_id+requirement_type),同 program 连续")
