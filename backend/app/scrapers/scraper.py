"""
UQ Course Profile Scraper
==========================
把 UQ course-profiles 页面解析成统一 schema 的 JSON。

设计目标:
  1. 结构化字段 -> 给 SQL / LLM-to-SQL 用(精确过滤,如 semester=S2、has_exam=false)
  2. 派生字段     -> 高频问题直接命中,不用 LLM 也能答
  3. 文本聚合字段 -> 给向量库做 embedding(语义模糊检索)

用法:
    python uq_scraper.py CSSE1001-21206-7620
    python uq_scraper.py --file course_ids.txt        # 批量
"""
from __future__ import annotations
import re
import sys
import json
import time
import argparse
from dataclasses import dataclass, asdict, field

import requests
from bs4 import BeautifulSoup

BASE = "https://course-profiles.uq.edu.au/course-profiles/{}"
HEADERS = {"User-Agent": "Mozilla/5.0 (course-kb-scraper)"}


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
@dataclass
class Assessment:
    task: str
    category: str            # e.g. "Examination", "Computer Code"
    weight: float | None     # 百分比数值,可能为 None
    hurdle: bool = False


@dataclass
class Course:
    # --- 身份 ---
    code: str                       # CSSE1001
    offering_id: str                # 完整 url slug,做主键
    title: str
    # --- 开课信息(SQL 过滤核心) ---
    study_period: str               # "Semester 1, 2026"
    semester: str | None            # 派生: S1 / S2 / Summer ...
    year: int | None
    location: str | None
    attendance_mode: str | None     # In Person / External / ...
    level: str | None               # Undergraduate / Postgraduate
    units: float | None
    coordinating_unit: str | None
    coordinator: str | None
    incompatible: list[str] = field(default_factory=list)
    prerequisite_raw: str = ""            # 先修原文(AND/OR/括号有意义,作权威源)
    prerequisite_parsed: dict | None = None   # 解析树;无字段=None;不可解析={op:raw}
    # --- 评估 ---
    assessments: list[Assessment] = field(default_factory=list)
    has_exam: bool = False          # 派生: 是否含 Examination
    has_hurdle: bool = False        # 派生: 是否含 hurdle
    # --- 文本(给向量库) ---
    description: str = ""
    learning_outcomes: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    learning_activities: list[dict] = field(default_factory=list)
    # --- embedding 用的聚合文本 ---
    search_blob: str = ""

    def to_dict(self):
        d = asdict(self)
        return d


# --------------------------------------------------------------------------- #
# 解析辅助
# --------------------------------------------------------------------------- #
def _text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip() if node else ""


def _field_after_label(soup, label: str) -> str | None:
    """
    UQ 页面里很多信息是 '标签\n值' 的结构(dt/dd 或相邻文本)。
    用文本搜索定位标签,取其后紧邻的值。
    """
    el = soup.find(string=re.compile(rf"^\s*{re.escape(label)}\s*$", re.I))
    if not el:
        return None
    # 标签的下一个有内容的兄弟/父级下一个元素
    nxt = el.find_parent()
    if nxt:
        sib = nxt.find_next_sibling()
        if sib:
            v = _text(sib)
            if v:
                return v
    return None


def _normalise_semester(study_period: str) -> tuple[str | None, int | None]:
    """'Semester 1, 2026 (...)' -> ('S1', 2026)"""
    sem = None
    m = re.search(r"Semester\s*([12])", study_period, re.I)
    if m:
        sem = f"S{m.group(1)}"
    elif re.search(r"summer", study_period, re.I):
        sem = "Summer"
    yr = None
    ym = re.search(r"(20\d{2})", study_period)
    if ym:
        yr = int(ym.group(1))
    return sem, yr


# --------------------------------------------------------------------------- #
# 先修解析(纯函数,确定性;无法干净解析就退回 {op:raw},绝不臆造结构)
# --------------------------------------------------------------------------- #
_CODE_RE = re.compile(r"^[A-Z]{2,4}\d{4}$")


def _expand_abbrev_codes(raw: str) -> str:
    """UQ 缩写码展开:'ACCT1110 or 1111 or 2112' -> 补全裸 4 位数为最近前缀的码。"""
    last_prefix = [None]

    def repl(m):
        tok = m.group(0)
        cm = re.match(r"([A-Z]{2,4})\d{4}", tok)
        if cm:
            last_prefix[0] = cm.group(1)
            return tok
        return (last_prefix[0] + tok) if last_prefix[0] else tok   # 裸 4 位数

    return re.sub(r"[A-Z]{2,4}\d{4}|\b\d{4}\b", repl, raw)


def parse_prereq(raw: str) -> dict | None:
    """先修原文 -> AND/OR 树。

    语法:expr = code | '(' expr ')' | expr (and|or) expr;'+'/'&'/','=and(逗号保守按与)。
    含任何非课程码/连接词的未知词(如 'units of'/'Permission')或括号不配 -> {op:raw}。
    空 -> None(无先修)。'or' 比 'and' 松,括号优先。
    """
    original = (raw or "").strip()
    if not original:
        return None
    expanded = _expand_abbrev_codes(original)
    toks = re.findall(r"[A-Z]{2,4}\d{4}|\(|\)|\+|&|,|[A-Za-z]+|\d+", expanded)
    norm: list[tuple[str, str]] = []
    for t in toks:
        if _CODE_RE.match(t):
            norm.append(("code", t))
        elif t in ("(", ")"):
            norm.append((t, t))
        elif t in ("+", "&", ",") or t.lower() == "and":
            norm.append(("and", t))
        elif t.lower() == "or":
            norm.append(("or", t))
        else:                               # 未知词/数字 -> 无法安全解析
            return {"op": "raw", "unparsed": original}
    if not any(k == "code" for k, _ in norm):
        return {"op": "raw", "unparsed": original}
    try:
        tree, i = _parse_or(norm, 0)
        if i != len(norm):
            return {"op": "raw", "unparsed": original}
        return tree
    except (IndexError, ValueError):
        return {"op": "raw", "unparsed": original}


def _parse_or(toks, i):
    left, i = _parse_and(toks, i)
    children = [left]
    while i < len(toks) and toks[i][0] == "or":
        right, i = _parse_and(toks, i + 1)
        children.append(right)
    return (left if len(children) == 1 else {"op": "or", "children": children}), i


def _parse_and(toks, i):
    left, i = _parse_atom(toks, i)
    children = [left]
    while i < len(toks) and toks[i][0] == "and":
        right, i = _parse_atom(toks, i + 1)
        children.append(right)
    return (left if len(children) == 1 else {"op": "and", "children": children}), i


def _parse_atom(toks, i):
    if i >= len(toks):
        raise ValueError("unexpected end")
    kind, val = toks[i]
    if kind == "code":
        return {"op": "course", "code": val}, i + 1
    if kind == "(":
        node, i = _parse_or(toks, i + 1)
        if i >= len(toks) or toks[i][0] != ")":
            raise ValueError("missing )")
        return node, i + 1
    raise ValueError(f"unexpected token {val}")


# --------------------------------------------------------------------------- #
# 主解析
# --------------------------------------------------------------------------- #
def parse(html: str, offering_id: str) -> Course:
    soup = BeautifulSoup(html, "html.parser")

    # --- 标题 & code:形如 "Introduction to Software Engineering (CSSE1001)" ---
    h1 = soup.find("h1")
    title_raw = _text(h1)
    code_m = re.search(r"\(([A-Z]{2,4}\d{4})\)", title_raw)
    code = code_m.group(1) if code_m else offering_id.split("-")[0]
    title = re.sub(r"\s*\([A-Z]{2,4}\d{4}\)\s*$", "", title_raw).strip()

    # --- 概览字段 ---
    study_period = _field_after_label(soup, "Study period") or ""
    # study period 在概览区可能是简写,优先用正文 "Semester 1, 2026 (...)"
    sp_full = soup.find(string=re.compile(r"Semester\s*[12].*20\d{2}"))
    if sp_full:
        study_period = _text(sp_full.find_parent() or sp_full).strip() or study_period
    semester, year = _normalise_semester(study_period)

    location = _field_after_label(soup, "Location")
    attendance = _field_after_label(soup, "Attendance mode")
    level = _field_after_label(soup, "Study level")
    units_raw = _field_after_label(soup, "Units")
    units = float(units_raw) if units_raw and units_raw.replace(".", "").isdigit() else None
    coord_unit = _field_after_label(soup, "Coordinating unit")

    # --- coordinator ---
    coordinator = None
    cc = soup.find(string=re.compile(r"Course coordinator", re.I))
    if cc:
        blk = cc.find_parent()
        if blk:
            nxt = blk.find_next(string=re.compile(r"\b(Dr|Prof|A/Prof|Mr|Ms|Mrs)\b"))
            coordinator = _text(nxt.find_parent()) if nxt else None

    # --- incompatible ---
    incompatible: list[str] = []
    inc = soup.find(string=re.compile(r"^\s*Incompatible\s*$", re.I))
    if inc:
        scope = inc.find_parent()
        if scope:
            # 课程码可能在标签后的第一或第二个 <p>,逐个扫描直到命中
            for p in scope.find_all_next("p", limit=4):
                codes = re.findall(r"[A-Z]{2,4}\d{4}", _text(p))
                if codes:
                    incompatible = codes
                    break

    # --- prerequisite(与 Incompatible 同结构;锚定 ^Prerequisite(s)$,不误命中
    #     'Recommended prerequisite' / 'Companion';保留全文,AND/OR 有意义) ---
    prerequisite_raw = ""
    pre = soup.find(string=re.compile(r"^\s*Prerequisite[s]?\s*$", re.I))
    if pre:
        scope = pre.find_parent()
        if scope:
            first_nonempty = ""
            for p in scope.find_all_next("p", limit=4):
                t = re.sub(r"^You'?ll need to complete.*?:\s*", "", _text(p), flags=re.I).strip()
                if not t:
                    continue
                if not first_nonempty:
                    first_nonempty = t
                if re.search(r"[A-Z]{2,4}\d{4}", t):  # 优先取含课程码的那段
                    first_nonempty = t
                    break
            prerequisite_raw = first_nonempty
    prerequisite_parsed = parse_prereq(prerequisite_raw)

    # --- description(概览正文) ---
    description = ""
    ov = soup.find(string=re.compile(r"Course overview", re.I))
    if ov:
        paras = []
        for p in ov.find_all_next("p", limit=6):
            t = _text(p)
            if len(t) > 60:
                paras.append(t)
        description = " ".join(paras[:2])

    # --- learning outcomes ---
    los: list[str] = []
    for lo in soup.find_all(string=re.compile(r"^\s*LO\d", re.I)):
        parent = lo.find_parent()
        nxt = parent.find_next_sibling() if parent else None
        if nxt:
            los.append(_text(nxt))

    # --- assessments(解析评估表)---
    assessments: list[Assessment] = []
    has_exam = has_hurdle = False
    for table in soup.find_all("table"):
        head = _text(table.find("tr"))
        if "Assessment task" in head and "Weight" in head:
            for tr in table.find_all("tr")[1:]:
                cells = [_text(td) for td in tr.find_all(["td", "th"])]
                if len(cells) < 3:
                    continue
                category, task, weight_raw = cells[0], cells[1], cells[2]
                wm = re.search(r"(\d+(?:\.\d+)?)\s*%", weight_raw)
                weight = float(wm.group(1)) if wm else None
                is_hurdle = bool(re.search(r"hurdle", task, re.I))
                is_exam = "exam" in category.lower()
                if is_exam:
                    has_exam = True
                if is_hurdle:
                    has_hurdle = True
                assessments.append(Assessment(
                    task=re.sub(r"(Hurdle|Identity Verified|In-person)", "", task).strip(),
                    category=category, weight=weight, hurdle=is_hurdle))
            break

    # --- learning activities(结构化保留全部活动:period/type/topic/LO)---
    learning_activities: list[dict] = []
    topics: list[str] = []          # lecture 周次主题摘要,给向量检索用
    for table in soup.find_all("table"):
        head = _text(table.find("tr"))
        if "Topic" in head and "Activity type" in head:
            for tr in table.find_all("tr")[1:]:
                cells = [_text(td) for td in tr.find_all(["td", "th"])]
                if not cells:
                    continue
                # 列数可能是 3(period/type/topic)或 2(period 为空)
                if len(cells) >= 3:
                    period, activity, topic_cell = cells[0], cells[1], cells[2]
                elif len(cells) == 2:
                    period, activity, topic_cell = "", cells[0], cells[1]
                else:
                    period, activity, topic_cell = "", "", cells[0]

                # 拆出关联的 learning outcomes(如 "Learning outcomes: L01, L02")
                lo_codes: list[str] = []
                lo_split = re.split(r"Learning outcomes\s*:?", topic_cell, flags=re.I)
                topic_text = lo_split[0]
                if len(lo_split) > 1:
                    lo_codes = re.findall(r"L0?\d+", lo_split[1])
                # 去掉前缀的 "Week N" / activity 名重复(兼容复数,如 Practical/Practicals)
                topic_text = re.sub(r"^\s*Week\s*\d+\s*", "", topic_text)
                if activity:
                    topic_text = re.sub(rf"^\s*{re.escape(activity)}s?\b\s*", "",
                                        topic_text, flags=re.I)
                topic_text = topic_text.strip(" .")

                learning_activities.append({
                    "period": period,
                    "activity_type": activity,
                    "topic": topic_text,
                    "learning_outcomes": lo_codes,
                })
                # lecture 类的主题进 topics 摘要
                if activity and re.search(r"lecture", activity, re.I) and topic_text:
                    topics.append(topic_text)
            break

    # --- embedding 聚合文本 ---
    search_blob = " | ".join(filter(None, [
        f"{code} {title}", description,
        " ".join(a["topic"] for a in learning_activities if a["topic"]),
        " ".join(los),
    ]))

    return Course(
        code=code, offering_id=offering_id, title=title,
        study_period=study_period, semester=semester, year=year,
        location=location, attendance_mode=attendance, level=level,
        units=units, coordinating_unit=coord_unit, coordinator=coordinator,
        incompatible=incompatible,
        prerequisite_raw=prerequisite_raw, prerequisite_parsed=prerequisite_parsed,
        assessments=assessments,
        has_exam=has_exam, has_hurdle=has_hurdle,
        description=description, learning_outcomes=los, topics=topics,
        learning_activities=learning_activities,
        search_blob=search_blob,
    )


def fetch(offering_id: str, retries: int = 3) -> Course:
    url = BASE.format(offering_id)
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return parse(r.text, offering_id)
        except Exception as e:
            if i == retries - 1:
                raise
            time.sleep(2 * (i + 1))


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*", help="offering id, e.g. CSSE1001-21206-7620")
    ap.add_argument("--file", help="每行一个 offering id 的清单文件")
    ap.add_argument("--out", default="data/courses.jsonl", help="输出 JSONL")
    ap.add_argument("--delay", type=float, default=1.0, help="请求间隔秒")
    args = ap.parse_args()

    ids = list(args.ids)
    if args.file:
        with open(args.file) as f:
            ids += [ln.strip() for ln in f if ln.strip()]

    if not ids:
        ap.error("请提供至少一个 offering id 或 --file")

    with open(args.out, "w", encoding="utf-8") as out:
        for oid in ids:
            try:
                course = fetch(oid)
                out.write(json.dumps(course.to_dict(), ensure_ascii=False) + "\n")
                print(f"[ok] {oid} -> {course.code} {course.title} "
                      f"(sem={course.semester}, exam={course.has_exam})")
            except Exception as e:
                print(f"[fail] {oid}: {e}", file=sys.stderr)
            time.sleep(args.delay)
    print(f"\n写入 {args.out}")


if __name__ == "__main__":
    main()