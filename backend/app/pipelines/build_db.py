"""
build_db.py — stage three: create tables + load data
Read courses.jsonl and write into Postgres (pgvector). The embedding column is
left empty and filled by embed.py.
Safe to re-run: upsert by offering_id, no duplicate inserts, and it does not
overwrite an embedding that was already computed.

Usage:
    python build_db.py --in courses.jsonl
"""
from __future__ import annotations
import os
import re
import json
import pathlib
import argparse
from datetime import date

import psycopg

from app.core.config import DSN
EMBED_DIM = 1024  # bge-m3
YEAR_LONG_MIN_DAYS = 240  # teaching span threshold: a normal semester is about 120 days, the longest short term about 165 days, a year-long course about 270 days

DDL = f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS courses (
    offering_id         TEXT PRIMARY KEY,
    code                TEXT,
    title               TEXT,
    study_period        TEXT,
    semester            TEXT,
    year                INTEGER,
    location            TEXT,
    attendance_mode     TEXT,
    level               TEXT,
    units               REAL,
    coordinating_unit   TEXT,
    coordinator         TEXT,
    has_exam            BOOLEAN,
    has_hurdle          BOOLEAN,
    incompatible        JSONB,
    assessments         JSONB,
    learning_outcomes   JSONB,
    topics              JSONB,
    learning_activities JSONB,
    description         TEXT,
    search_blob         TEXT,
    embedding           VECTOR({EMBED_DIM})
);
CREATE INDEX IF NOT EXISTS idx_courses_code     ON courses(code);
CREATE INDEX IF NOT EXISTS idx_courses_semester ON courses(semester);
CREATE INDEX IF NOT EXISTS idx_courses_year     ON courses(year);
CREATE INDEX IF NOT EXISTS idx_courses_level    ON courses(level);
CREATE INDEX IF NOT EXISTS idx_courses_has_exam ON courses(has_exam);

-- 阶段三b 先修(已建库则幂等补列)
ALTER TABLE courses ADD COLUMN IF NOT EXISTS prerequisite_raw    TEXT;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS prerequisite_parsed JSONB;

-- 年课标记:派生自 study_period 授课跨度(跨连续两学期),供排课器平摊学分
ALTER TABLE courses ADD COLUMN IF NOT EXISTS is_year_long BOOLEAN;

-- 课程类型:派生自 title + assessment 类别(见 classify_course_type)。
-- 取值 placement/thesis/research,其余为默认 coursework(非空,便于结构化排除某类课)。
ALTER TABLE courses ADD COLUMN IF NOT EXISTS course_type TEXT NOT NULL DEFAULT 'coursework';

-- 期中考试标记:据 assessment 命名派生(见 classify_midterm)。has/none/unknown 三态。
-- unknown = 有考试但命名判不出时点,查询「没有期中」时须显式排除并提示,绝不当 none。
-- 默认 unknown(保守):重建前的存量行不会被误当成「确定没有期中」。
ALTER TABLE courses ADD COLUMN IF NOT EXISTS midterm_status TEXT NOT NULL DEFAULT 'unknown';
CREATE INDEX IF NOT EXISTS idx_courses_midterm ON courses(midterm_status);

-- 小组/团队评估标记:据 assessment 命名派生(见 classify_group)。has/none/unknown 三态。
-- 服务「想避开 group work」的学生,召回优先:has=任一考核含 group/team 信号;none=有考核但无一命中;
-- unknown=没有考核项数据(判不出)。默认 unknown(保守):存量行不会被误当成「确定没有 group」。
ALTER TABLE courses ADD COLUMN IF NOT EXISTS group_status TEXT NOT NULL DEFAULT 'unknown';
CREATE INDEX IF NOT EXISTS idx_courses_group ON courses(group_status);

-- 当前学期开课标记(按 code 派生,见 pipelines/backfill_offerings):semester 文本列按 offering 存且不可靠
-- (S1 行是 2026,S2 行多为去年 2025 代理,真实 S2 开课清单在 S2_CODES 文件而非列),
-- 故 S1/S2 的「本期是否开课」走这两个按 code 统一的标记列。默认 false,须跑 backfill_offerings 填充。
ALTER TABLE courses ADD COLUMN IF NOT EXISTS offered_s1 BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS offered_s2 BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS idx_courses_offered_s1 ON courses(offered_s1);
CREATE INDEX IF NOT EXISTS idx_courses_offered_s2 ON courses(offered_s2);
"""

COLS = ["offering_id", "code", "title", "study_period", "semester", "year",
        "location", "attendance_mode", "level", "units", "coordinating_unit",
        "coordinator", "has_exam", "has_hurdle", "incompatible", "assessments",
        "learning_outcomes", "topics", "learning_activities", "description",
        "search_blob", "prerequisite_raw", "prerequisite_parsed", "is_year_long",
        "course_type", "midterm_status", "group_status"]
JSON_COLS = {"incompatible", "assessments", "learning_outcomes", "topics",
             "learning_activities"}

# Course type inference (deterministic, precision first): title keywords are the main signal, assessment category adds recall.
# placement only trusts the title (a pure assessment Placement category is noisy: many normal courses include a placement assessment step).
_TYPE_PLACEMENT_RE = re.compile(r"\b(placement|internship|practicum|fieldwork)\b", re.I)
_TYPE_THESIS_RE = re.compile(r"\b(thesis|dissertation)\b", re.I)
_TYPE_RESEARCH_RE = re.compile(r"\bresearch\b", re.I)
# No lab/laboratory: taught courses like 'Laboratory Skills in Genetic Research' would be mislabelled (precision first).
_TYPE_RESEARCH_CTX_RE = re.compile(r"\b(project|honours|honour|capstone)\b", re.I)

# Midterm exam timing (deterministic): UQ rarely uses "Midterm"; the standard name for a midterm is "In-Semester Exam";
# an in-class test counts as a midterm by business rule. The standard name for a final is "Final" / "End of Semester".
_MID_RE = re.compile(r"mid[\s-]*sem|mid[\s-]*term|midterm|in[\s-]*semester|in[\s-]*class", re.I)
_FINAL_RE = re.compile(r"final|end[\s-]*of[\s-]*semester|end[\s-]*sem", re.I)
_EXAMLIKE_RE = re.compile(r"\b(exam|quiz|test)\b", re.I)

# Group/team assessment (deterministic, recall first — opposite to the precision-first rule of course_type). The UQ standard marker is
# "Team or group-based" at the end of the task; some courses only write Group Project / Group Presentation / Team... in the task and do not add
# the standard marker, but they still count as group. This column serves students who want to avoid group work: missing a case is what hurts them, so prefer to exclude more.
_GROUP_RE = re.compile(r"team or group-based|\bgroup\b|\bteam\b", re.I)


def is_year_long(study_period: str | None) -> bool | None:
    """Decide whether a course is year-long from the teaching span in study_period (crossing two consecutive semesters).

    For a value like "Semester 1, 2026 (23/02/2026 - 21/11/2026)": a span >= YEAR_LONG_MIN_DAYS
    means year-long. If the date range cannot be parsed, return None (the caller counts it explicitly, instead of silently treating it as not year-long).
    """
    if not study_period:
        return None
    m = re.search(r"\((\d{2})/(\d{2})/(\d{4})\s*-\s*(\d{2})/(\d{2})/(\d{4})\)", study_period)
    if not m:
        return None
    d1, mo1, y1, d2, mo2, y2 = (int(x) for x in m.groups())
    try:
        span = (date(y2, mo2, d2) - date(y1, mo1, d1)).days
    except ValueError:
        return None
    return span >= YEAR_LONG_MIN_DAYS


def classify_course_type(title: str | None, assessments: list | None) -> str:
    """Decide the course type deterministically from title + assessment category, returning placement/thesis/research/coursework.

    Precision first (prefer to miss a label rather than wrongly kill a normal course, matching the student-facing rule "refuse over wrong"):
      - placement: title contains placement/internship/practicum/fieldwork (a pure assessment signal is not trusted)
      - thesis: title contains thesis/dissertation, or an assessment whose category is just 'Thesis' on its own
      - research: title contains research and also contains project/honours/capstone (this filters out "teaching research methods" courses)
      - otherwise: coursework
    Priority is placement > thesis > research (for example "Industry Research Placement" is judged as placement).

    UQ's assessment.category is a comma-joined multi-value string (such as 'Project, Thesis'). Only an assessment that
    equals exactly Thesis after splitting on commas counts, so a taught course with a final exam (such as STAT3008, where one assessment has category='Project, Thesis') is not mislabelled as a thesis course.
    """
    t = title or ""
    if _TYPE_PLACEMENT_RE.search(t):
        return "placement"
    standalone_thesis = any(
        {p.strip().lower() for p in (a.get("category") or "").split(",") if p.strip()} == {"thesis"}
        for a in (assessments or [])
    )
    if _TYPE_THESIS_RE.search(t) or standalone_thesis:
        return "thesis"
    if _TYPE_RESEARCH_RE.search(t) and _TYPE_RESEARCH_CTX_RE.search(t):
        return "research"
    return "coursework"


def classify_midterm(assessments: list | None) -> str:
    """Decide deterministically whether there is a midterm exam from the assessment names, returning has/none/unknown.

    Only exam/quiz/test type assessments are considered (category contains exam, or task contains exam/quiz/test):
      - has: any task matches the midterm word family (in-semester/mid-sem/mid-term/midterm/in-class)
      - unknown: there are such assessments but the name does not tell the timing (neither midterm nor final, such as a bare 'Exam'/'Quiz')
      - none: no such assessment, or all of them are finals (final/end-of-semester)
    When the timing cannot be decided it always becomes unknown, never silently treated as "no midterm" (student-facing "refuse over wrong").
    """
    items = [
        (a.get("task") or "")
        for a in (assessments or [])
        if "exam" in (a.get("category") or "").lower() or _EXAMLIKE_RE.search(a.get("task") or "")
    ]
    if any(_MID_RE.search(t) for t in items):
        return "has"
    if any(not _MID_RE.search(t) and not _FINAL_RE.search(t) for t in items):
        return "unknown"
    return "none"


def classify_group(assessments: list | None) -> str:
    """Decide deterministically whether there is a group/team assessment from the assessment task names, returning has/none/unknown.

    Recall first (opposite to the precision-first rule of classify_course_type): this column serves students who want to avoid group work,
    and missing a course that has group work as none is what hurts them, so both the standard marker and a bare group/team word count as has.
      - has: any task matches a group/team signal
      - none: there are assessments but none match
      - unknown: no assessment data (cannot decide, never treated as none)
    """
    items = [(a.get("task") or "") for a in (assessments or [])]
    if not items:
        return "unknown"
    if any(_GROUP_RE.search(t) for t in items):
        return "has"
    return "none"


def load_exam_overrides(path: str) -> dict:
    """Read the manually verified exam-status correction table: offering_id -> {has_exam?, midterm_status?, ...}.

    The automatic classifiers (classify_midterm / the scraper's has_exam) can only judge by assessment category/name, and cannot
    decide exams that are held in the exam period or invigilated on site, like quiz/OSCA/Viva, that are "not called Examination". After checking each ECP by hand, the decision is written into
    data/exam_overrides.jsonl (one offering per line) and applied at build time, so that a full rebuild / watch_s2 incremental load
    will not roll the manually verified result back to the automatic value.
    A missing file returns empty (the override is an optional enhancement); a line that fails JSON parsing or lacks offering_id raises directly (not silent).
    """
    p = pathlib.Path(path)
    if not p.exists():
        return {}
    out: dict[str, dict] = {}
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        oid = d.get("offering_id")
        if not oid:
            raise ValueError(f"exam_overrides 第 {i} 行缺 offering_id:{line!r}")
        out[oid] = d
    return out


def apply_exam_override(c: dict, ov: dict) -> list[str]:
    """Apply the override onto the course row c (in place), returning the list of changed field names (for counting/reporting).

    Only the two columns has_exam / midterm_status may be overridden; a value equal to the current one does not count as a change."""
    changed: list[str] = []
    for field in ("has_exam", "midterm_status"):
        if field in ov and c.get(field) != ov[field]:
            c[field] = ov[field]
            changed.append(field)
    return changed


def row_values(c: dict) -> list:
    vals = []
    for col in COLS:
        v = c.get(col)
        if col == "prerequisite_parsed":
            # None must be stored as JSON null (no prerequisite), not as [] — it has to be distinguished from "not scraped"
            v = json.dumps(v, ensure_ascii=False)
        elif col in JSON_COLS:
            v = json.dumps(v if v is not None else [], ensure_ascii=False)
        vals.append(v)
    return vals


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="infile", default="data/courses.jsonl")
    ap.add_argument("--overrides", default="data/exam_overrides.jsonl",
                    help="人工核实的考试状态修正表(offering_id->has_exam/midterm_status)")
    args = ap.parse_args()

    rows = [json.loads(l) for l in open(args.infile, encoding="utf-8") if l.strip()]
    overrides = load_exam_overrides(args.overrides)
    placeholders = ",".join(["%s"] * len(COLS))
    updates = ",".join(f"{c}=EXCLUDED.{c}" for c in COLS if c != "offering_id")
    sql = (f"INSERT INTO courses ({','.join(COLS)}) VALUES ({placeholders}) "
           f"ON CONFLICT (offering_id) DO UPDATE SET {updates}")

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
            n = n_yl = n_unparsed = ov_hits = 0
            n_type: dict[str, int] = {}
            n_mid: dict[str, int] = {}
            n_group: dict[str, int] = {}
            for c in rows:
                yl = is_year_long(c.get("study_period"))
                c["is_year_long"] = yl
                if yl is None:
                    n_unparsed += 1
                elif yl:
                    n_yl += 1
                ct = classify_course_type(c.get("title"), c.get("assessments"))
                c["course_type"] = ct
                n_type[ct] = n_type.get(ct, 0) + 1
                ms = classify_midterm(c.get("assessments"))
                c["midterm_status"] = ms
                gs = classify_group(c.get("assessments"))
                c["group_status"] = gs
                n_group[gs] = n_group.get(gs, 0) + 1
                ov = overrides.get(c.get("offering_id"))
                if ov and apply_exam_override(c, ov):     # manual correction (after automatic classification, so it is not rolled back)
                    ov_hits += 1
                n_mid[c["midterm_status"]] = n_mid.get(c["midterm_status"], 0) + 1
                cur.execute(sql, row_values(c))
                n += 1
        conn.commit()
    print(f"灌入 {n} 行 -> courses(DSN={DSN});年课 {n_yl} 门;"
          f"study_period 无法解析 {n_unparsed} 门(is_year_long=NULL);"
          f"course_type {n_type};midterm_status {n_mid};group_status {n_group};"
          f"人工核实修正命中 {ov_hits} 个 offering(共录入 override {len(overrides)} 条)")
    from app.pipelines import backfill_offerings
    backfill_offerings.main()


if __name__ == "__main__":
    main()
