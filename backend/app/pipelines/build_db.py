"""
build_db.py — 阶段三:建表 + 灌库
读 courses.jsonl 写入 Postgres(pgvector)。embedding 列留空,由 embed.py 填充。
可重复运行:按 offering_id upsert,不会重复插入,也不会覆盖已算好的 embedding。

用法:
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
YEAR_LONG_MIN_DAYS = 240  # 授课跨度阈值:标准学期约 120 天、最长短学期约 165 天、年课约 270 天

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
"""

COLS = ["offering_id", "code", "title", "study_period", "semester", "year",
        "location", "attendance_mode", "level", "units", "coordinating_unit",
        "coordinator", "has_exam", "has_hurdle", "incompatible", "assessments",
        "learning_outcomes", "topics", "learning_activities", "description",
        "search_blob", "prerequisite_raw", "prerequisite_parsed", "is_year_long",
        "course_type", "midterm_status", "group_status"]
JSON_COLS = {"incompatible", "assessments", "learning_outcomes", "topics",
             "learning_activities"}

# 课程类型推断(确定性,精度优先):title 关键词为主信号,assessment 类别补召回。
# placement 仅认 title(纯 assessment 的 Placement 类别噪声大:很多正常课带实习考核环节)。
_TYPE_PLACEMENT_RE = re.compile(r"\b(placement|internship|practicum|fieldwork)\b", re.I)
_TYPE_THESIS_RE = re.compile(r"\b(thesis|dissertation)\b", re.I)
_TYPE_RESEARCH_RE = re.compile(r"\bresearch\b", re.I)
# 不含 lab/laboratory:'Laboratory Skills in Genetic Research' 等讲授课会被误标(精度优先)。
_TYPE_RESEARCH_CTX_RE = re.compile(r"\b(project|honours|honour|capstone)\b", re.I)

# 期中考试时点(确定性):UQ 极少用 "Midterm",期中标准命名是 "In-Semester Exam";
# in-class 课堂测验按业务规则计入期中。期末标准命名是 "Final" / "End of Semester"。
_MID_RE = re.compile(r"mid[\s-]*sem|mid[\s-]*term|midterm|in[\s-]*semester|in[\s-]*class", re.I)
_FINAL_RE = re.compile(r"final|end[\s-]*of[\s-]*semester|end[\s-]*sem", re.I)
_EXAMLIKE_RE = re.compile(r"\b(exam|quiz|test)\b", re.I)

# 小组/团队评估(确定性,召回优先——与 course_type 的精度优先相反)。UQ 标准标记是 task 末尾的
# "Team or group-based";部分课只在 task 里写 Group Project / Group Presentation / Team... 而没打
# 标准标记,一并按 group 计。本列服务「想避开 group work」的学生:漏判才坑人,故宁可多排除。
_GROUP_RE = re.compile(r"team or group-based|\bgroup\b|\bteam\b", re.I)


def is_year_long(study_period: str | None) -> bool | None:
    """据 study_period 授课起止跨度判断是否年课(横跨连续两学期)。

    形如 "Semester 1, 2026 (23/02/2026 - 21/11/2026)":跨度 >= YEAR_LONG_MIN_DAYS
    即年课。无法解析出日期区间返回 None(交调用方显式计数,不静默当成非年课)。
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
    """据 title + assessment 类别确定性判课程类型,返回 placement/thesis/research/coursework。

    精度优先(宁可漏标也别错杀正常课,符合 student-facing「refuse over wrong」):
      - placement:title 含 placement/internship/practicum/fieldwork(不认纯 assessment 信号)
      - thesis:title 含 thesis/dissertation,或某项考核的 category 单独就是 'Thesis'
      - research:title 含 research 且同时含 project/honours/capstone(滤掉「讲授研究方法」类课)
      - 其余:coursework
    优先级 placement > thesis > research(如「Industry Research Placement」判 placement)。

    UQ 的 assessment.category 是逗号拼接的多值串(如 'Project, Thesis')。只看「拆逗号后
    单独等于 Thesis」的考核,避免把带期末考的授课课(如 STAT3008,某项 category='Project, Thesis')误标论文课。
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
    """据考核命名确定性判是否含期中考试,返回 has/none/unknown。

    只看 exam/quiz/test 类考核(category 含 exam,或 task 含 exam/quiz/test):
      - has:任一 task 命中期中词族(in-semester/mid-sem/mid-term/midterm/in-class)
      - unknown:有此类考核但命名判不出时点(既非期中也非期末,如裸 'Exam'/'Quiz')
      - none:无此类考核,或全部是期末(final/end-of-semester)
    判不出时点一律归 unknown,绝不静默当成「没有期中」(student-facing「refuse over wrong」)。
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
    """据考核 task 命名确定性判是否含小组/团队评估,返回 has/none/unknown。

    召回优先(与 classify_course_type 的精度优先相反):本列服务「想避开 group work」的学生,
    把有 group 的课漏判成 none 才会坑人,故标准标记 + 裸 group/team 词都算 has。
      - has:任一 task 命中 group/team 信号
      - none:有考核项但无一命中
      - unknown:没有考核项数据(判不出,绝不当 none)
    """
    items = [(a.get("task") or "") for a in (assessments or [])]
    if not items:
        return "unknown"
    if any(_GROUP_RE.search(t) for t in items):
        return "has"
    return "none"


def load_exam_overrides(path: str) -> dict:
    """读人工核实的考试状态修正表:offering_id -> {has_exam?, midterm_status?, ...}。

    自动分类器(classify_midterm / scraper 的 has_exam)只能据考核 category/命名判断,判不出
    在考试周或现场监考的 quiz/OSCA/Viva 这类「不叫 Examination 的考试」。逐门核 ECP 后把判定写进
    data/exam_overrides.jsonl(每行一个 offering),建库时套用,确保全量重建 / watch_s2 增量入库
    都不会把人工核实结果还原成自动分类值。
    文件缺失返回空(override 可选增强);某行 JSON 解析失败或缺 offering_id 直接抛错(不静默)。
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
    """把 override 套到课程行 c(原地改),返回被改动的字段名列表(供计数/报告)。

    只允许覆盖 has_exam / midterm_status 两列;值与现有相同不算改动。"""
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
            # None 必须存 JSON null(无先修),不能退化成 []——要与「未爬到」区分
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
                if ov and apply_exam_override(c, ov):     # 人工核实修正(在自动分类之后,确保不被还原)
                    ov_hits += 1
                n_mid[c["midterm_status"]] = n_mid.get(c["midterm_status"], 0) + 1
                cur.execute(sql, row_values(c))
                n += 1
        conn.commit()
    print(f"灌入 {n} 行 -> courses(DSN={DSN});年课 {n_yl} 门;"
          f"study_period 无法解析 {n_unparsed} 门(is_year_long=NULL);"
          f"course_type {n_type};midterm_status {n_mid};group_status {n_group};"
          f"人工核实修正命中 {ov_hits} 个 offering(共录入 override {len(overrides)} 条)")


if __name__ == "__main__":
    main()
