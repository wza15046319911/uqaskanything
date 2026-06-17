"""
program_lookup.py — program-level question helper (pure SQL)
Reads program_course / programs tables to answer "which programs use this course",
"which courses a program has", and "find a program by name".
All read-only SELECT, psycopg3 style; importable functions, with a real-DB self-test at the end of the file.

Public functions:
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
    """Which programs use this course as core/elective. Join programs to get title.

    collapse=False (default, old behavior): order by requirement_type, title. Each program may
        appear in many rows due to multiple via_plan/course_list (row blow-up).
    collapse=True: fold and dedup by (program_id, requirement_type), aggregate course_list/via_plan/
        plan_subtype into lists (each deduped), and give the original row count row_count. Sort puts
        program_id first, so multiple requirement_type rows of the same program stay next to each other.
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

    # collapse=True: aggregate by (program_id, requirement_type) in SQL,
    # use array_agg(DISTINCT ...) to collect multi-row course_list/via_plan/plan_subtype into lists.
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
    """Courses of a program; direct_only takes only via_plan=''; left join courses for title; order by course_code.

    Note: courses.code is not a primary key (one course has multiple offerings), so the join may blow up
    row count. Take DISTINCT on (program_id, course_code, requirement_type, course_list, via_plan, plan_subtype)
    then add title, to avoid duplicate rows.

    Assumption: DISTINCT ON folds multiple offerings of the same course_code into one row, which implies "same code, same title".
    Right now each code in the DB has a unique title, so this assumption holds. If later the same code has multiple titles,
    the ascending c.title at the end of ORDER BY keeps the alphabetically smallest row (deterministic, never drops data at random).
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
    """Course codes the program bans without condition (program_exclude table, e.g. BCompSc bans MATH1040)."""
    rows = conn.execute(
        "SELECT course_code FROM program_exclude WHERE program_id = %s ORDER BY course_code",
        (program_id,)).fetchall()
    return [r[0] for r in rows]


def is_excluded(conn, program_id: str, course_code: str) -> bool:
    """Whether the program bans a given course without condition."""
    return conn.execute(
        "SELECT 1 FROM program_exclude WHERE program_id = %s AND course_code = %s LIMIT 1",
        (program_id, course_code.upper())).fetchone() is not None


def programs_excluding(conn, course_code: str) -> list[tuple[str, str]]:
    """All programs that ban a given course, returns [(program_id, title)]."""
    rows = conn.execute(
        "SELECT pe.program_id, p.title FROM program_exclude pe "
        "LEFT JOIN programs p ON p.program_id = pe.program_id "
        "WHERE pe.course_code = %s ORDER BY p.title", (course_code.upper(),)).fetchall()
    return [(r[0], r[1]) for r in rows]


def aux_rules(conn, program_id: str) -> list[dict]:
    """All extra rules of the program (banned courses / level cap / plan conflicts ...), for display."""
    row = conn.execute("SELECT aux_rules FROM programs WHERE program_id = %s", (program_id,)).fetchone()
    return row[0] if row and row[0] else []


def has_plan_level_core(conn, program_id: str) -> bool:
    """Whether the program has plan-level (via_plan<>'') core courses. The direct_only p2c query does not show these
    (the core courses of a major/direction), used to add a hint line in the answer."""
    row = conn.execute(
        "SELECT 1 FROM program_course WHERE program_id = %s "
        "AND requirement_type = 'core' AND via_plan <> '' LIMIT 1", (program_id,)).fetchone()
    return row is not None


def find_program(conn, name_substr: str) -> list[tuple[str, str]]:
    """Fuzzy-find a program by title ILIKE, returns [(program_id, title)].

    Sort is deterministic: exact same name > title starts with the name > shorter title (more specific) > alphabetical.
    The caller takes progs[0], so a substring query (e.g. 'Master of Data Science') must put the standalone program
    before the combined program ('Bachelor of X / Master of Data Science'), otherwise it answers the wrong thing.

    Empty/whitespace-only short-circuits to []: otherwise '%%' would match every program (a meaningless full-table return).
    """
    if name_substr is None or not name_substr.strip():
        return []
    # Normalize inner whitespace: user/LLM may type extra spaces between words (e.g. 'Bachelor of  Information Technology'),
    # while DB titles use single spaces, and ILIKE is space-sensitive so the whole string would miss. Collapse to single space before matching.
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

        # ---- Reproduction-case checks (assert failure raises, never silent) ----
        print("\n=== 复现用例验证 ===")
        assert find_program(conn, "") == [], "find_program('') 应短路返回 []"
        assert find_program(conn, "  ") == [], "find_program('  ') 应短路返回 []"
        assert len(find_program(conn, "Computer Science")) > 0, "Computer Science 仍应命中"
        # When a substring query matches several, the standalone program (exact same name) must rank first, else progs[0] picks the combined program
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
        # Collapsed row count = distinct (program_id, requirement_type), and distinct program_id count matches
        assert len({d["program_id"] for d in collapsed}) == distinct_pids, \
            "折叠前后 distinct program_id 数应一致"
        # Rows of the same program_id must be contiguous (sort puts program_id first)
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
