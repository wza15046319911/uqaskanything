"""
qa.py — main entry for course QA (integrates planner + retrieval + program_lookup + answer)
Natural-language question -> planner produces a query plan -> route by mode -> answer generates a grounded Chinese reply.

mode routing:
  filter   -> retrieval.filter_search (structured filter)
  semantic -> retrieval.semantic_search (vector + keyword RRF)
  hybrid   -> retrieval.hybrid_search (structured + semantic)
  program  -> program_lookup (course_to_programs / program_to_courses)
  kb       -> retrieval.kb_search (knowledge base FAQ/date/policy, decided up front by planner classification)
  guide    -> retrieval.guide_search (single-course subjective experience / guide, triggered by experience intent; fact intent short-circuits first, falls back to course_detail when there is no guide in the store)
  empty    -> question too broad / cannot form a retrieval condition (graceful fallback when planner raises ValueError)

Usage:
    python qa.py "CS有哪些课程没有考试"
    python qa.py "CSSE1001是哪些专业的必修"
    python qa.py --no-gen "..."        # retrieve only, do not generate an answer
"""
from __future__ import annotations
import os
import re
import argparse

import psycopg

from app.services import planner, retrieval, program_lookup, answer, answerability, simulator

from app.core.config import DSN
from app.core import i18n
from app.core.i18n import Lang
ANSWER_CAP = 20      # max number of courses fed to the answer model (more is pointless and lengthens the prompt)
PROGRAM_CAP = 15     # max number of programs course_to_programs feeds to the answer model
KB_PREFER_SIM = 0.55  # when course semantic top sim is below this and KB recall is stronger, switch to the knowledge base (FAQ/article)
KB_STRONG_SIM = 0.62  # when filter hits empty, only switch when KB top sim reaches this high threshold (guards against weak-relevance mis-switch, e.g. campus course queries)
# _kb_or_none soft threshold: a near-threshold hit of 0.60-0.62 is not refused outright, the answerability dual gate (entity/year + P2 LLM) decides.
# Saves real questions where "the correct page sits just under the 0.62 threshold" (word-order jitter of a Chinese query against an English KB, e.g. "how to issue an enrolment certificate" 0.618);
# made-up ones are still blocked by the dual gate (entity absent / LLM gate). Discrimination precision belongs to answerability, not simply lowering the threshold to let everything through.
KB_SOFT_SIM = 0.60
# Guide attached to a course_detail (intro-type) answer: the guide is already physically scoped to this one course,
# so any of its chunks is on-topic; keep the floor at 0 (show the experience layer whenever a guide exists), and only
# rank by the question. Fact-intent questions still get no guide at all (gated in _retrieve, red line 1/3).
GUIDE_ATTACH_SIM = 0.0
# Date/time-point intent words: asking "when / which day" rather than the course itself -> even if filter hits a course, switch to the knowledge base (academic calendar)
_DATE_INTENT = re.compile(r"什么时候|何时|哪天|几号|日期|开学|开课|放假|census|截止|deadline|when|start\s*date", re.I)
# Real course-filter dimensions: filters with these keys count as "querying courses"; semester alone is a time restriction, not a course filter.
# Deliberately only these 6 dimensions (word-for-word with the old regex, excludes midterm/group/course_type/semester); adding dimensions needs a separate proposal.
_COURSE_DIM_KEYS = {"level", "units", "has_exam", "has_hurdle", "location", "attendance_mode"}
# EMPTY_MSG (problem too broad) is bilingual via i18n.MESSAGES["empty_msg"]; REQ_LABEL via i18n.label_req.
REQ_LABEL = {"core": "必修", "elective": "选修"}


def _requirement(question: str) -> str | None:
    """Deterministically decide from the question whether it wants core or elective (elective keyword wins over core, three states)."""
    if any(w in question for w in ("选修", "elective")):
        return "elective"
    if any(w in question for w in ("核心", "必修", "compulsory", "core")):
        return "core"
    return None


def _c2p_rank(r: dict) -> int:
    """c2p dedup priority: true core 0 > pick-one core 1 > elective 2 (smaller wins)."""
    if r.get("requirement_type") == "core":
        return 0 if not r.get("equiv_group") else 1
    return 2


def _ans_c2p(code: str, program_facts: list, req: str | None = None, lang: Lang = "zh") -> str:
    """Deterministic answer for course_to_programs (enumeration, no LLM, zero hallucination).

    Distinguishes three states: true core (core and not pick-one) / pick-one core (core and in an equivalence
    group, can swap for an equivalent course) / elective. program_facts is already deduped by program_id (one row per program).
    req filter: 'core' lists only core + pick-one core, 'elective' lists only elective, None lists all three states.
    """
    if not program_facts:
        return (f"{code} 不在任何已收录专业的课表中。" if lang == "zh"
                else f"{code} is not listed in the course list of any indexed program.")
    hard, oneof, elec = set(), set(), set()
    oneof_sizes: set[int] = set()
    for p in program_facts:
        t = p["title"]
        if p.get("requirement_type") == "core":
            if p.get("equiv_group"):
                oneof.add(t)
                oneof_sizes.add(len(p["equiv_group"].split("|")))
            else:
                hard.add(t)
        else:
            elec.add(t)
    oneof -= hard                    # when a program has both true core and pick-one, true core takes precedence
    elec -= (hard | oneof)
    if req == "core":                # user restricts to core: do not list elective (pick-one core still counts as core)
        elec = set()
    elif req == "elective":          # user restricts to elective: do not list core or pick-one core
        hard, oneof = set(), set()
    k = next(iter(oneof_sizes)) if len(oneof_sizes) == 1 else None
    n = len(hard) + len(oneof) + len(elec)
    if lang == "en":
        choice = (f"choose 1 of {k}" if k else "multi-select") + " core"
        scope = {"core": "compulsory", "elective": "elective"}.get(req, "")
        bd = ([f"{len(hard)} compulsory"] if req != "elective" else []) \
            + ([f"{len(oneof)} {choice}"] if oneof else []) \
            + ([f"{len(elec)} elective"] if req != "core" else [])
        scope_str = f" {scope}" if scope else ""
        out = [f"{code} appears in the{scope_str} course lists of {n} programs (" + ", ".join(bd) + ")."]
        if hard:
            out.append(" Compulsory in: " + ", ".join(sorted(hard)[:8]) + (f", and {len(hard)} more." if len(hard) > 8 else "."))
        if oneof:
            out.append(f" {choice} in: " + ", ".join(sorted(oneof)[:6]) + (f", and {len(oneof)} more." if len(oneof) > 6 else "."))
        if elec:
            out.append(" Elective in: " + ", ".join(sorted(elec)[:6]) + (f", and {len(elec)} more." if len(elec) > 6 else "."))
        return "".join(out)
    word = (i18n.choice_word(k, "zh") if k else "多选一") + "核心"
    scope = {"core": "必修", "elective": "选修"}.get(req, "")
    bd = ([f"必修 {len(hard)} 个"] if req != "elective" else []) \
        + ([f"{word} {len(oneof)} 个"] if oneof else []) \
        + ([f"选修 {len(elec)} 个"] if req != "core" else [])
    head = f"{code} 出现在 {n} 个专业的{scope}课表中(" + "、".join(bd) + ")。"
    out = [head]
    if hard:
        out.append("必修于:" + "、".join(sorted(hard)[:8]) + (f" 等 {len(hard)} 个。" if len(hard) > 8 else "。"))
    if oneof:
        out.append(f" {word}于:" + "、".join(sorted(oneof)[:6]) + (f" 等 {len(oneof)} 个。" if len(oneof) > 6 else "。"))
    if elec:
        out.append(" 选修于:" + "、".join(sorted(elec)[:6]) + (f" 等 {len(elec)} 个。" if len(elec) > 6 else "。"))
    return "".join(out)


def _collapse_slots(courses: list) -> list[dict]:
    """Fold an equivalence (pick-one) group into 1 slot by (course_list, equiv_group).
    Returns [{codes:[...], titles:[...], is_group:bool}]; standalone courses each form their own slot, order stable."""
    slots: list[dict] = []
    groups: dict = {}
    for c in courses:
        g = c.get("equiv_group") or ""
        if not g:
            slots.append({"codes": [c["code"]], "titles": [c.get("title")], "is_group": False})
            continue
        key = (c.get("course_list"), g)
        slot = groups.get(key)
        if slot is None:
            slot = {"codes": [], "titles": [], "is_group": True}
            groups[key] = slot
            slots.append(slot)
        slot["codes"].append(c["code"])
        slot["titles"].append(c.get("title"))
    return slots


def _ans_p2c(title: str, req: str | None, courses: list, lang: Lang = "zh") -> str:
    """Deterministic answer for program_to_courses (enumeration, no LLM). Equivalence groups fold into 1 course, worded by group size,
    and always listed separately (to avoid being cut off by the single-course truncation)."""
    if not courses:
        return (f"未找到 {title} 的相关课程。" if lang == "zh"
                else f"No courses found for {title}.")
    slots = _collapse_slots(courses)
    n = len(slots)
    groups = [s for s in slots if s["is_group"]]
    singles = [s for s in slots if not s["is_group"]]
    sizes = {len(s["codes"]) for s in groups}
    SHOW = 12

    if lang == "en":
        parts = [s["codes"][0] + (f" ({s['titles'][0]})" if s["titles"][0] else "") for s in singles[:SHOW]]
        if len(singles) > SHOW:
            parts.append("etc.")
        seg = []
        if parts:
            seg.append(", ".join(parts))
        if groups:
            seg.append("pick one from each group: " + ", ".join(" or ".join(s["codes"]) for s in groups))
        grp_note = f" (incl. {len(groups)} pick-one group{'s' if len(groups) != 1 else ''})" if groups else ""
        scope = i18n.label_req(req, "en")
        scope_str = f" {scope}" if scope else ""
        return f"{title} has {n}{scope_str} courses{grp_note}: {'; '.join(seg)}."

    word = i18n.choice_word(next(iter(sizes)), "zh") if len(sizes) == 1 else "多选一"
    parts = [s["codes"][0] + (f"({s['titles'][0]})" if s["titles"][0] else "") for s in singles[:SHOW]]
    if len(singles) > SHOW:
        parts.append("等")
    seg = []
    if parts:
        seg.append("、".join(parts))
    if groups:
        seg.append(f"可{word}项:" + "、".join(" 或 ".join(s["codes"]) for s in groups))
    grp_note = f"(其中 {len(groups)} 门{word})" if groups else ""
    return f"{title} 的{REQ_LABEL.get(req, '')}课共 {n} 门{grp_note}:{';'.join(seg)}。"


def _titles_for(conn, codes) -> dict:
    """Batch-fetch course titles (DISTINCT ON code; codes with no offering record are not in the result)."""
    codes = list(dict.fromkeys(codes))
    if not codes:
        return {}
    rows = conn.execute(
        "SELECT DISTINCT ON (code) code, title FROM courses WHERE code = ANY(%s) ORDER BY code",
        (codes,)).fetchall()
    return {r[0]: r[1] for r in rows}


def _structure_or_none(conn, program_id: str):
    """Get the program's structured enumeration (simulator.structure_overview); a build failure does not break the main flow,
    return None so the upper layer uses flat enumeration (rule 19: log the failure explicitly, no silent fake success)."""
    try:
        return simulator.PlanSimulator(conn, program_id).structure_overview()
    except Exception as e:
        print(f"[qa] structure_overview 失败,退回扁平枚举: {type(e).__name__}: {e}")
        return None


# req -> which course-group kinds to include (elective includes open rules; core is core only; None lists all)
_REQ_KINDS = {"core": {"core"}, "elective": {"elective", "open"}}


def _fmt_group(g: dict, titles: dict, show: int = 8, lang: Lang = "zh") -> str:
    """Course-list text for one course group: open rules give a scope note, otherwise list the first show courses (with names) + remainder."""
    if g["open_scope"]:
        if lang == "en":
            return "any course in the program list" if g["open_scope"] == "program" else "any course university-wide"
        return "程序课表内任选" if g["open_scope"] == "program" else "全校任意课程"
    cs = g["courses"]
    if lang == "en":
        parts = [f"{c} ({titles[c]})" if titles.get(c) else c for c in cs[:show]]
        tail = f", and {len(cs)} more" if len(cs) > show else ""
        return ", ".join(parts) + tail
    parts = [f"{c}({titles[c]})" if titles.get(c) else c for c in cs[:show]]
    tail = f" 等 {len(cs)} 门" if len(cs) > show else ""
    return "、".join(parts) + tail


def _ans_p2c_structured(title: str, req: str | None, overview: dict, titles: dict,
                        lang: Lang = "zh") -> str:
    """Deterministic answer for a program with a direction structure: list grouped by official block + direction (major/field), zero hallucination, no LLM."""
    kinds = _REQ_KINDS.get(req, {"core", "elective", "open"})
    groups = [g for g in overview["groups"] if g["kind"] in kinds]
    by_plan: dict[str, list] = {}
    order: list[str] = []
    for g in groups:
        if g["plan_name"]:
            if g["plan_name"] not in by_plan:
                by_plan[g["plan_name"]] = []
                order.append(g["plan_name"])
            by_plan[g["plan_name"]].append(g)
    general = [g for g in groups if not g["plan_name"]]
    codes = {c for g in groups for c in g["courses"]}

    if lang == "en":
        if not groups:
            scope_e = i18n.label_req(req, "en")
            return f"No {scope_e + ' ' if scope_e else ''}courses found for {title}."
        scope_e = i18n.label_req(req, "en") or "all"
        lines = [f"{title}'s {scope_e} courses, grouped by official block ({len(codes)} enumerable, "
                 f"plus an open elective pool; pick a direction then use the course planner for the full requirements):"]
        for g in general:
            cnt = f" ({i18n.n_courses(len(g['courses']), 'en')})" if g["courses"] else ""
            lines.append(f"· {g['title']}{cnt}: {_fmt_group(g, titles, lang='en')}")
        for pn in order:
            lines.append(f"[{pn} direction]")
            for g in by_plan[pn]:
                lines.append(f"· {g['title']} ({i18n.n_courses(len(g['courses']), 'en')}): {_fmt_group(g, titles, lang='en')}")
        return "\n".join(lines)

    if not groups:
        return f"未找到 {title} 的{REQ_LABEL.get(req, '')}课程。"
    scope = REQ_LABEL.get(req, "全部")
    lines = [f"{title} 的{scope}课按官方区块分组(共 {len(codes)} 门可枚举,"
             f"另含开放选修池;选定方向后可用选课模拟器看完整要求):"]
    for g in general:
        cnt = f"({len(g['courses'])} 门)" if g["courses"] else ""
        lines.append(f"· {g['title']}{cnt}:{_fmt_group(g, titles)}")
    for pn in order:
        lines.append(f"【{pn} 方向】")
        for g in by_plan[pn]:
            lines.append(f"· {g['title']}({len(g['courses'])} 门):{_fmt_group(g, titles)}")
    return "\n".join(lines)


def _engine_p2c(conn, title: str, req: str | None, overview: dict,
                lang: Lang = "zh") -> tuple[list, str]:
    """structure_overview -> (courses card list, grouping text). courses are deduped and carry requirement_type/block name,
    covering major-gated courses. Open rules have no enumerable codes, so they only go into the text, not the cards."""
    kinds = _REQ_KINDS.get(req, {"core", "elective", "open"})
    groups = [g for g in overview["groups"] if g["kind"] in kinds]
    code_grp: dict[str, dict] = {}
    order: list[str] = []
    for g in groups:
        for c in g["courses"]:
            if c not in code_grp:
                code_grp[c] = g
                order.append(c)
    titles = _titles_for(conn, order)
    courses = [{"code": c, "title": titles.get(c),
                "requirement_type": "core" if code_grp[c]["kind"] == "core" else "elective",
                "course_list": code_grp[c]["title"]} for c in order]
    return courses, _ans_p2c_structured(title, req, overview, titles, lang)


def _ans_program_filter(title: str, courses: list, lang: Lang = "zh") -> str:
    """Deterministic answer for a combined query (program scope + structured filter): flat-list the hit courses, no LLM, zero hallucination."""
    SHOW = 20
    if lang == "en":
        if not courses:
            return f"No courses in {title}'s course list match the filter."
        parts = [f"{c['code']} ({c['title']})" if c.get("title") else c["code"]
                 for c in courses[:SHOW]]
        tail = f", and {len(courses)} more" if len(courses) > SHOW else ""
        return f"{title}'s course list has {i18n.n_courses(len(courses), 'en')} matching the filter: " + ", ".join(parts) + tail + "."
    if not courses:
        return f"{title} 的课表中没有符合条件的课程。"
    parts = [f"{c['code']}({c['title']})" if c.get("title") else c["code"]
             for c in courses[:SHOW]]
    tail = f" 等 {len(courses)} 门" if len(courses) > SHOW else ""
    return f"{title} 课表中符合条件的共 {len(courses)} 门:" + "、".join(parts) + tail + "。"


def _ans_low_burden(courses: list, lang: Lang = "zh") -> str:
    """Deterministic answer for "low load / chill" queries: already sorted by objective assessment load (no exam / no hurdle / fewest assessment items).
    Honestly states the system does not judge difficulty/pass rate (red line 1: no difficulty data, never make it up), no LLM."""
    SHOW = 20
    if lang == "en":
        if not courses:
            return ("No course satisfies both 'no exam' and 'no hurdle'. Try relaxing the filter, "
                    "e.g. only 'no exam' or only 'no hurdle'.")
        parts = [f"{c['code']} ({c['title']})" if c.get("title") else c["code"]
                 for c in courses[:SHOW]]
        tail = (f", etc. — {len(courses)} courses in total" if len(courses) > SHOW
                else f" — {len(courses)} courses in total")
        return ("Sorted by objective assessment load (all have no exam and no hurdle, fewest assessment "
                "items first): " + ", ".join(parts) + tail
                + ". Note: the system does not judge course difficulty or pass rate; this is ranked only "
                  "by assessment structure, so assess it yourself with the course profile (ECP) and your background.")
    if not courses:
        return ("没有同时满足「无考试且无 hurdle」的课。可以放宽条件,比如只要「没有考试」"
                "或「没有 hurdle」再试。")
    parts = [f"{c['code']}({c['title']})" if c.get("title") else c["code"]
             for c in courses[:SHOW]]
    tail = f" 等共 {len(courses)} 门" if len(courses) > SHOW else f",共 {len(courses)} 门"
    return ("按客观考核负担排序(均无考试、无 hurdle,考核项由少到多):"
            + "、".join(parts) + tail
            + "。注:系统不判断课程难度或通过率,以上仅按考核结构排序,"
              "请结合课程大纲(ECP)与你的基础自行评估。")


def _ans_permit(code: str, title: str, excluded: bool, owns: list, lang: Lang = "zh") -> str:
    """Deterministic answer for "can a program take a course" (based on the program_exclude ban table, zero hallucination)."""
    if lang == "en":
        if excluded:
            return f"No. {title} explicitly gives no credit (No credit will be given for {code}) — taking it earns no credit."
        if owns:
            kind = "core/compulsory" if any(r["requirement_type"] == "core" for r in owns) else "elective"
            article = "an" if kind[0] in "aeiou" else "a"
            return f"Yes. {title} does not exclude {code}, and it is in the program's course list (as {article} {kind} course)."
        return (f"{title} does not list {code} as excluded; but it is also not in the program's specified "
                f"course list. Whether it counts as a general elective depends on the units/level distribution "
                f"rules — this database does not yet cover that judgement.")
    if excluded:
        return f"不能。{title} 明确规定不计学分(No credit will be given for {code})—— 修了也拿不到学分。"
    if owns:
        kind = "核心/必修" if any(r["requirement_type"] == "core" for r in owns) else "选修"
        return f"可以。{title} 未禁修 {code},且它就在该专业课表里(作为{kind}课)。"
    return (f"{title} 未把 {code} 列为禁修课;但它也不在该专业的指定课表中,"
            f"能否作为通选(general elective)计入要看学分/层级分布规则——本库暂未覆盖该判定。")


def _empty_note(filters: dict | None, lang: Lang = "zh") -> str:
    """When the filter hits empty and filters specify an attendance mode/campus value that "does not exist in the DB at all", give a deterministic clear hint
    (instead of a vague "too broad"): so the student knows the data itself has no such value, not that the question is wrong. Enums come from planner's live cache."""
    if not filters:
        return ""
    am = filters.get("attendance_mode")
    if am and str(am).strip().lower() not in planner._ENUM_CACHE.get("attendance_mode", set()):
        if lang == "en":
            return (f"All courses indexed here are taught In Person; there is no course data for the "
                    f"'{am}' attendance mode. Please check the delivery mode of a specific course on the official UQ course page.")
        return (f"本库收录的课程授课模式均为面授(In Person),暂无「{am}」授课模式的课程数据;"
                f"请到 UQ 官方课程页核对具体课程的授课方式。")
    loc = filters.get("location")
    if loc and str(loc).strip().lower() not in planner._ENUM_CACHE.get("location", set()):
        if lang == "en":
            return (f"There is no course data for the '{loc}' campus in this database. Please check the "
                    f"campus name or try an indexed campus.")
        return f"本库暂无「{loc}」校区的课程数据,请确认校区名称或换用已收录校区再试。"
    return ""


def _retrieve(conn, question: str, lang: Lang = "zh") -> dict:
    """Retrieve + route, return a structured result (without the LLM-generated answer); the program-mode deterministic answer goes in prog_answer.
    When mode='empty' the other fields are empty. Shared by run and run_stream, to avoid duplicating the whole routing logic.
    lang threads down so deterministic answers (prog_answer / det_answer / status_note / empty note) follow the question language."""
    try:
        schema = planner.build_schema_doc(conn)
        p = planner.plan(question, schema, conn)
    except ValueError as e:                     # cannot plan a course query -> try the knowledge base first, then fall back gracefully
        chunks = _kb_or_none(conn, question)
        if chunks:
            return {"plan": None, "mode": "kb",
                    "meta": "kb(无法规划课程查询,转知识库)", "courses": [],
                    "program_facts": None, "prog_answer": None, "chunks": chunks}
        return {"plan": None, "mode": "empty", "meta": str(e), "courses": [],
                "program_facts": None, "prog_answer": None, "chunks": []}

    mode = p["mode"]
    courses: list[dict] = []
    program_facts = None
    meta = ""
    prog_answer = None      # program-mode deterministic answer (no LLM)
    det_answer = None       # other deterministic answer slot (e.g. low load / chill); if non-empty, run uses it directly, bypassing the LLM

    # kb up-front routing: planner already judged it a school-affairs/policy/date question -> go straight to the knowledge base, do not touch the course DB.
    # When chunks is empty, the upper layer answer_kb issues the refusal (with official link), not degrading to a broad empty.
    if mode == "kb":
        return {"plan": p, "mode": "kb", "meta": "kb(分类→知识库)", "courses": [],
                "program_facts": None, "prog_answer": None,
                "chunks": _kb_or_none(conn, question, p.get("kb_query"))}

    # Course guide: planner judged it a single-course subjective-experience question (experience intent + course code; fact intent has already been short-circuited first).
    # Physical isolation: query only course_guides; if that course has no guide in the store -> gracefully fall back to course_detail (no error, no empty answer, red line 3 / Risk 4).
    if mode == "guide":
        code = p["course_code"]
        chunks = retrieval.guide_search(conn, code, question)
        if chunks:
            return {"plan": p, "mode": "guide", "meta": f"guide {code}({len(chunks)} 块)",
                    "courses": [], "program_facts": None, "prog_answer": None,
                    "chunks": chunks, "course": None}
        course = retrieval.course_detail(conn, code)
        if not course:
            return {"plan": p, "mode": "empty", "meta": f"guide 无攻略且未找到课程 {code}",
                    "courses": [], "program_facts": None, "prog_answer": None, "chunks": []}
        return {"plan": p, "mode": "course_detail", "meta": f"guide 无攻略,回退 course_detail {code}",
                "courses": [], "program_facts": None, "prog_answer": None,
                "chunks": [], "course": course}

    # Single-course detail: planner already judged it "intro/prereq/assessment of one course" -> fetch that course's full info.
    if mode == "course_detail":
        course = retrieval.course_detail(conn, p["course_code"])
        if not course:
            return {"plan": p, "mode": "empty", "meta": f"未找到课程 {p['course_code']}",
                    "courses": [], "program_facts": None, "prog_answer": None, "chunks": []}
        # Intro-type detail also surfaces the experience layer (guide) appended after the facts; fact-intent
        # questions (prereq / assessment weight / date / hurdle / units) never recall a guide (red line 1/3).
        guide_chunks: list[dict] = []
        if not planner.is_fact_intent(question):
            guide_chunks = retrieval.guide_search(conn, p["course_code"], question, min_sim=GUIDE_ATTACH_SIM)
        meta = f"course_detail {p['course_code']}" + (f"+guide({len(guide_chunks)} 块)" if guide_chunks else "")
        return {"plan": p, "mode": "course_detail", "meta": meta,
                "courses": [], "program_facts": None, "prog_answer": None,
                "chunks": [], "course": course, "guide_chunks": guide_chunks}

    cu = p.get("coord_units") or None           # discipline -> faculty restriction (deterministic, parameterized SQL), None = no limit
    cu_note = f" @units{cu}" if cu else ""
    if mode == "filter":
        try:                                    # filter_search raises ValueError on empty filters, degrade to empty instead of giving the student a 500
            ex_title = p.get("exclude_title") or None
            if p.get("both_semesters"):
                # "satisfies both S1 and S2": cross-semester conjunction, a hit needs the same course to have a matching offering in each semester
                courses = retrieval.filter_search_both_semesters(
                    conn, p["filters"] or None, coord_units=cu, exclude_title=ex_title)
            else:
                courses = retrieval.filter_search(conn, p["filters"],
                                                  order_by=p.get("order") or "code", coord_units=cu,
                                                  exclude_title=ex_title)
        except ValueError as e:
            return {"plan": p, "mode": "empty", "meta": f"空 filters 被安全网拦截:{e}",
                    "courses": [], "program_facts": None, "prog_answer": None, "chunks": []}
        ex_note = f" NOT_TITLE{p['exclude_title']}" if p.get("exclude_title") else ""
        meta = (("WHERE S1∩S2 都满足 " if p.get("both_semesters") else "WHERE ")
                + (retrieval.describe_where(p["filters"]) or "(仅两学期都开)")
                + (f" ORDER {p['order']}" if p.get("order") else "") + cu_note + ex_note)
        # "low load / chill": no difficulty data, deterministically produce an objective-load answer (red line 1, do not let the LLM invent difficulty)
        if p.get("order") == "assessments_asc":
            det_answer = _ans_low_burden(courses, lang)
        # Hits empty and asks for an attendance mode/campus value the DB lacks -> deterministic clear hint (instead of a vague fallback / wrong KB switch)
        if not courses and not det_answer:
            det_answer = _empty_note(p["filters"], lang)
    elif mode == "semantic":
        courses = retrieval.semantic_search(conn, p["semantic_query"], coord_units=cu)
        meta = f"semantic='{p['semantic_query']}'" + cu_note
    elif mode == "hybrid":
        try:                                    # on filters error, fall back to pure semantic search, to keep topic recall
            courses = retrieval.hybrid_search(conn, p["filters"] or None, p["semantic_query"],
                                              coord_units=cu)
            meta = f"WHERE {retrieval.describe_where(p['filters'])} + semantic='{p['semantic_query']}'" + cu_note
        except ValueError:
            courses = retrieval.semantic_search(conn, p["semantic_query"], coord_units=cu)
            meta = f"semantic='{p['semantic_query']}'(filters 被安全网拦截,已降级)" + cu_note
    elif mode == "program":
        if p.get("direction") == "permit":
            code = p["course_code"]
            progs = program_lookup.find_program(conn, p.get("program_name") or "")
            if progs:
                pid, title = progs[0]
                excluded = program_lookup.is_excluded(conn, pid, code)
                owns = [r for r in program_lookup.programs_for_course(conn, code)
                        if r["program_id"] == pid]
                program_facts = {"program": title, "program_id": pid, "course": code,
                                 "excluded": excluded, "in_program": bool(owns)}
                meta = f"{code} @ program='{title}' 能否修(禁课={excluded})"
                prog_answer = _ans_permit(code, title, excluded, owns, lang)
            else:
                name = p.get("program_name") or ""
                meta = f"未找到 program '{name}'"
                prog_answer = i18n.t("program_not_found", lang, name=name)
        elif p.get("direction") == "program_to_courses":
            progs = program_lookup.find_program(conn, p.get("program_name") or "")
            if progs:
                pid, title = progs[0]
                req = _requirement(question)
                # For a combined query (has where), include plan-level (major/direction) courses, so "program scope" covers the full course list;
                # a pure program course-list query still lists only direct courses (via_plan='', keeping the original plan-level hint).
                rows = program_lookup.courses_for_program(
                    conn, pid, requirement_type=req, direct_only=not p.get("filters"))
                courses = [{**c, "code": c.get("course_code")} for c in rows]  # normalize the code key
                pick = f"(从 {len(progs)} 个匹配中选第一个)" if len(progs) > 1 else ""
                if p.get("filters"):
                    # Combined query: within the program's course list (including the plan level), deterministically filter by structured conditions (take the intersection).
                    # Use filter_search rows to bring back semester/units/level/exam fields for the frontend cards.
                    prog_codes = {c["code"] for c in courses}
                    try:
                        filtered = retrieval.filter_search(conn, p["filters"])
                    except ValueError as e:
                        return {"plan": p, "mode": "empty",
                                "meta": f"空 filters 被安全网拦截:{e}", "courses": [],
                                "program_facts": None, "prog_answer": None, "chunks": []}
                    courses = [c for c in filtered if c["code"] in prog_codes]
                    filter_desc = retrieval.describe_where(p["filters"])
                    program_facts = {"program": title, "program_id": pid,
                                     "requirement": req or "all", "filter": filter_desc}
                    meta = f"program='{title}'{pick} ∩ WHERE {filter_desc} 命中 {len(courses)} 门"
                    prog_answer = _ans_program_filter(title, courses, lang)
                else:
                    # A program with a direction structure (has major/field) goes through the simulator rule engine for a full per-direction enumeration, covering
                    # major-gated courses (a flat program_course only has via_plan='' direct courses and would miss them); a program with no direction
                    # structure (e.g. 5522/5519) keeps flat enumeration (its direct course list is already complete). On engine failure, fall back to flat.
                    ov = _structure_or_none(conn, pid)
                    if ov and any(g["plan_name"] for g in ov["groups"]):
                        courses, prog_answer = _engine_p2c(conn, title, req, ov, lang)
                        program_facts = {"program": title, "program_id": pid,
                                         "requirement": req or "all", "structured": True}
                        meta = (f"program='{title}'{pick} 结构化枚举(按方向),"
                                f"{len(courses)} 门可枚举")
                    else:
                        program_facts = {"program": title, "program_id": pid,
                                         "requirement": req or "all"}
                        meta = f"program='{title}'{pick} 的{REQ_LABEL.get(req, '')}课程"
                        prog_answer = _ans_p2c(title, req, courses, lang)
                        # B: when the program also has plan-level (major/direction) core courses, add a hint (the direct query does not show these)
                        if req != "elective" and program_lookup.has_plan_level_core(conn, pid):
                            prog_answer += (
                                " 注:该专业含 major/方向,其核心课需选定方向后确定,可用选课模拟器查看。"
                                if lang == "zh" else
                                " Note: this program has majors/directions; its core courses are determined "
                                "after choosing a direction — use the course planner to view them.")
                    # Banned-course note: courses the program explicitly gives no credit for (shared by both paths; a structured multi-line answer separates with a newline)
                    ex = program_lookup.excluded_courses(conn, pid)
                    if ex:
                        sep = "\n" if "\n" in prog_answer else " "
                        if lang == "en":
                            tail = f", and {len(ex)} more" if len(ex) > 8 else ""
                            prog_answer += f"{sep}This program excludes (no credit): {', '.join(ex[:8])}{tail}."
                        else:
                            tail = f" 等 {len(ex)} 门" if len(ex) > 8 else ""
                            prog_answer += f"{sep}该专业禁修(不计学分):{'、'.join(ex[:8])}{tail}。"
            else:
                name = p.get("program_name") or ""
                meta = f"未找到 program '{name}'"
                prog_answer = i18n.t("program_not_found", lang, name=name)
        else:  # course_to_programs
            code = p["course_code"]
            by_prog: dict = {}                          # one row per program; true core > pick-one core > elective
            for r in program_lookup.programs_for_course(conn, code):
                pid = r["program_id"]
                if pid not in by_prog or _c2p_rank(r) < _c2p_rank(by_prog[pid]):
                    by_prog[pid] = r
            program_facts = sorted(by_prog.values(),
                                   key=lambda r: (_c2p_rank(r), r["title"]))
            req = _requirement(question)
            meta = f"{code} 所属 program(共 {len(program_facts)}){REQ_LABEL.get(req, '')}"
            prog_answer = _ans_c2p(code, program_facts, req, lang)
            # Banned-course note: programs that explicitly ban this course
            excl_progs = program_lookup.programs_excluding(conn, code)
            if excl_progs:
                eg = excl_progs[0][1]
                prog_answer += (
                    f" 另有 {len(excl_progs)} 个专业明确禁修该课(不计学分),如 {eg}。"
                    if lang == "zh" else
                    f" Another {len(excl_progs)} programs explicitly exclude this course (no credit), e.g. {eg}.")

    # KB fallback: course retrieval is weak/empty and KB recall is stronger -> switch to KB FAQ/article.
    # - FAQs like census date / password reset make courses semantically recall low-relevance courses (sim 0.45~0.5),
    #   so do not only check "whether courses is empty", compare top sim instead (real course questions like machine learning have high top sim, unaffected).
    # - Date questions ("2026 start / census which day") are often misjudged as filter with empty course hits; use a high sim threshold to switch to KB,
    #   which both hits the academic calendar (sim≈0.66) and blocks weak-relevance mis-switches like "Gatton campus courses" (sim≈0.6).
    kb_chunks = None
    if mode in ("semantic", "hybrid"):
        courses_top = max((c.get("sim") or 0.0 for c in courses), default=0.0)
        if courses_top < KB_PREFER_SIM:
            cand = _kb_or_none(conn, question)
            if cand and cand[0]["sim"] > courses_top:
                kb_chunks = cand
    elif mode == "filter" and not det_answer:
        # filter hits empty, or "asks for a date (start/census) and where is only a time restriction (no course-filter dimension)"
        # -> most likely not a course query (planner took 2026/S1 as a structured condition); switch if KB recall is strong enough.
        # When a deterministic det_answer already exists (low load / non-enum empty hint), do not switch to KB: KB cannot help, and it would override the clear hint.
        date_q = bool(_DATE_INTENT.search(question))
        only_time = not any(k in (p.get("filters") or {}) for k in _COURSE_DIM_KEYS)
        if (not courses) or (date_q and only_time):
            cand = _kb_or_none(conn, question)
            if cand and cand[0]["sim"] >= KB_STRONG_SIM:
                kb_chunks = cand
    if kb_chunks:
        return {"plan": p, "mode": "kb", "meta": f"kb(课程检索弱/空转知识库;原 {meta})",
                "courses": [], "program_facts": None, "prog_answer": None, "chunks": kb_chunks}

    # "Filter by year from the first digit of code" now enters build_where SQL as the code_level slot (filter/hybrid/program combined),
    # no longer a Python post-filter; so all hits are already a deterministically year-filtered range.
    return {"plan": p, "mode": mode, "meta": meta,
            "courses": courses, "program_facts": program_facts,
            "prog_answer": prog_answer, "det_answer": det_answer, "chunks": [],
            "status_note": _status_note(conn, p.get("filters"), cu,
                                        both_semesters=p.get("both_semesters", False),
                                        exclude_title=p.get("exclude_title") or None, lang=lang)}


def _kb_or_none(conn, question: str, query_en: str | None = None) -> list:
    """Knowledge base semantic search + answerability gate; KB is an enhancing fallback layer.

    When query_en (the English KB query planner produces in kb mode) is non-empty, kb_search recalls by max(sim_zh, sim_en)
    (cross-language root-cause fix: corpus is English, a Chinese query jitters near the threshold); when empty it is single-language, behavior unchanged. The answerability gate
    still judges using the original Chinese question (fictional-entity/year checks do not depend on the translation).
    A retrieval failure (vector service flutter) only degrades gracefully to "no recall"; but an answerability gate rejection (fictional entity /
    year out of range) is a deterministic refusal, return [] so downstream KB_REFUSE takes over (shared by sync/stream paths, answer.py unchanged).
    Configuration errors like a missing word list are raised from answerable() and propagate up -- not mixed into the degrade except above and silenced (rule 19)."""
    try:
        chunks = retrieval.kb_search(conn, question, min_sim=KB_SOFT_SIM, query_en=query_en)
    except Exception:
        return []
    if not chunks:
        return []
    ok, reason = answerability.answerable(question, chunks)
    if not ok:
        print(f"[answerability] 拒答:{reason} | q={question!r}")
        return []
    # P2: after the deterministic gate passes, run the LLM answerability gate (catches Chinese fictional entities). LLM flutter/failure is fail-open (pass through),
    # otherwise one external-service error would wrongly refuse all real KB questions (breaks red line 3); only log, no silence (rule 19).
    try:
        ok2, reason2 = answerability.llm_answerable(question, chunks)
    except Exception as e:
        print(f"[answerability] LLM 门异常,fail-open 放行:{type(e).__name__}: {e} | q={question!r}")
        ok2 = True
    if not ok2:
        print(f"[answerability] LLM 拒答:{reason2} | q={question!r}")
        return []
    return chunks


# For "does not have X" three-state column (midterm_status / group_status) queries, the hint text for the excluded unknown rows.
# col -> the phrase in the hint that describes "why these courses cannot be judged"; when querying col='none', deterministically count the col='unknown' courses in the same range.
_UNKNOWN_NOTE_KINDS = {
    "midterm_status": {
        "zh": "的考核命名无法确定是否含期中考试",
        "en": "whose assessment naming makes it unclear whether they include a midterm exam",
    },
    "group_status": {
        "zh": "没有可解析的考核数据,无法确定是否含小组/团队评估",
        "en": "with no parseable assessment data, so whether they include group/team assessment is unclear",
    },
}


def _status_unknown_note(conn, filters: dict | None, col: str, coord_units=None,
                         both_semesters: bool = False, exclude_title=None, lang: Lang = "zh") -> str:
    """Deterministic fallback hint for "does not have X" (three-state column col='none') queries (rule 19: do not silently drop courses that cannot be judged).

    When filters has col='none', flip it to ='unknown' and count courses under the same conditions (the flipped dict round-trips through build_where,
    'unknown' is a valid enum value -- this is the safety guarantee that replaces the old guard_where re-validation).
    unknown = cannot tell whether it has that item, never counted into "does not have X", but the student must be told to check it themselves.
    The count uses the same range as the main query: filters already has code_level / coord_units / three-state etc. all conditions, and the whole flipped group counts in the same range via filter_search
    inside SQL; with both_semesters it also uses the two-semester conjunction count. On count failure, only degrade to not adding the hint, do not break the main flow."""
    if not filters or filters.get(col) != "none":
        return ""
    unk = dict(filters)
    unk[col] = "unknown"
    try:
        if both_semesters:
            rows = retrieval.filter_search_both_semesters(conn, unk, coord_units=coord_units,
                                                          exclude_title=exclude_title)
        else:
            rows = retrieval.filter_search(conn, unk, coord_units=coord_units,
                                           exclude_title=exclude_title)
    except Exception:
        return ""
    n = len(rows)
    if n == 0:
        return ""
    if lang == "en":
        return (f"\n\nNote: another {i18n.n_courses(n, 'en')} {_UNKNOWN_NOTE_KINDS[col]['en']}, not counted "
                f"in the list above; please check each against its course profile (ECP).")
    return (f"\n\n注:另有 {n} 门课{_UNKNOWN_NOTE_KINDS[col]['zh']},未计入上面的名单,"
            f"请到对应课程大纲(ECP)逐一核对。")


def _status_note(conn, filters, coord_units=None,
                 both_semesters: bool = False, exclude_title=None, lang: Lang = "zh") -> str:
    """Combine the unknown fallback hints of each three-state column (midterm / group); join one line per column hit."""
    return "".join(
        _status_unknown_note(conn, filters, col, coord_units,
                             both_semesters=both_semesters, exclude_title=exclude_title, lang=lang)
        for col in _UNKNOWN_NOTE_KINDS
    )


def run(conn, question: str, generate: bool = True, lang: Lang | None = None) -> dict:
    lang = lang or i18n.detect_lang(question)
    r = _retrieve(conn, question, lang)
    if r["mode"] == "empty":
        return {"plan": None, "mode": "empty", "meta": r["meta"], "courses": [],
                "program_facts": None, "chunks": [],
                "answer": i18n.t("empty_msg", lang) if generate else None}
    ans = None
    if generate:
        if r["mode"] == "kb":
            ans = answer.answer_kb(question, r["chunks"], lang=lang)
        elif r["mode"] == "guide":
            ans = answer.answer_guide(question, r["chunks"], lang=lang)
        elif r["mode"] == "course_detail":
            ans = answer.answer_course_detail(question, r.get("course"), lang=lang)
            if r.get("guide_chunks"):           # intro-type detail: append the unofficial experience layer after the facts
                ans += "\n\n" + answer.answer_guide(question, r["guide_chunks"], lang=lang)
        elif r["mode"] == "program":
            ans = r["prog_answer"]
        elif r.get("det_answer"):                # low load / chill: deterministic answer, bypasses the LLM (red line 1)
            ans = r["det_answer"]
        else:
            ans = answer.answer(question, r["courses"][:ANSWER_CAP],
                                _gen_facts(r["courses"], r["program_facts"]),
                                topical=r["mode"] in ("semantic", "hybrid"), lang=lang)
            if ans and r.get("status_note"):    # "no midterm / no group" queries deterministically add the unknown hint
                ans += r["status_note"]
    if r["mode"] == "program":                  # deterministic answer, not fed to the LLM, no retrieval context
        gen_ctx: list[str] = []
    elif r["mode"] in ("kb", "guide", "course_detail"):
        gen_ctx = answer.gen_contexts(r["mode"], chunks=r.get("chunks"), course=r.get("course"),
                                      question=question, lang=lang)
    else:                                       # filter/semantic/hybrid: align with the actual production input (capped + _gen_facts)
        gen_ctx = answer.gen_contexts(
            r["mode"], courses=r["courses"][:ANSWER_CAP],
            program_facts=_gen_facts(r["courses"], r["program_facts"]), lang=lang)
    return {"plan": r["plan"], "mode": r["mode"], "meta": r["meta"],
            "courses": r["courses"], "program_facts": r["program_facts"],
            "chunks": r.get("chunks", []), "course": r.get("course"),
            "answer": ans, "gen_context": gen_ctx}


def run_stream(conn, question: str, lang: Lang | None = None):
    """Streaming QA, yields (event, data) in order:
       ('meta', {mode, meta, courses, program_facts}) -> ('token', delta)... -> ('done', full answer).
       empty gives a fixed fallback sentence; program answers are deterministic (single block); other modes stream token by token + a closing guard."""
    lang = lang or i18n.detect_lang(question)
    r = _retrieve(conn, question, lang)
    mode = r["mode"]
    yield ("meta", {"mode": mode, "meta": r["meta"], "courses": r["courses"],
                    "program_facts": r["program_facts"], "chunks": r.get("chunks", []),
                    "course": r.get("course")})

    if mode == "empty":
        empty_msg = i18n.t("empty_msg", lang)
        yield ("token", empty_msg)
        yield ("done", empty_msg)
        return
    if mode == "program":
        ans = r["prog_answer"] or ""
        yield ("token", ans)
        yield ("done", ans)
        return
    if r.get("det_answer"):                      # low load / chill: deterministic answer, sent as a single block (no LLM, red line 1)
        ans = r["det_answer"]
        yield ("token", ans)
        yield ("done", ans)
        return
    if mode == "kb":                            # knowledge base: stream the body; sources go through meta.chunks, frontend renders source cards
        chunks = r["chunks"]
        if not chunks:
            refuse = i18n.t("kb_refuse", lang)
            yield ("token", refuse)
            yield ("done", refuse)
            return
        fixed = answer.fixed_kb_body(chunks, lang=lang)    # high-risk topics (census) use a deterministic template, no LLM streaming
        if fixed:
            yield ("token", fixed)
            yield ("done", fixed)
            return
        acc: list[str] = []
        for delta in answer.answer_kb_stream(question, chunks, lang=lang):
            acc.append(delta)
            yield ("token", delta)
        full = "".join(acc)
        if answer.is_empty_kb_answer(full):     # streaming empty-answer fallback: override done with the same retry+degrade as the non-streaming path
            full = answer.kb_answer_body(question, chunks, lang=lang)
        yield ("done", full)
        return
    if mode == "guide":                         # course guide experience: send the deterministic year / unofficial prefix first, then stream the experience-layer retelling (a no-guide case has already been turned into course_detail in _retrieve)
        acc: list[str] = []
        for delta in answer.answer_guide_stream(question, r["chunks"], lang=lang):
            acc.append(delta)
            yield ("token", delta)
        yield ("done", "".join(acc))
        return
    if mode == "course_detail":                 # single-course intro: stream the summary (structured facts go through meta.course frontend card)
        acc: list[str] = []
        for delta in answer.answer_course_detail_stream(question, r.get("course"), lang=lang):
            acc.append(delta)
            yield ("token", delta)
        if r.get("guide_chunks"):               # intro-type detail: append the unofficial experience layer after the facts
            sep = "\n\n"
            acc.append(sep)
            yield ("token", sep)
            for delta in answer.answer_guide_stream(question, r["guide_chunks"], lang=lang):
                acc.append(delta)
                yield ("token", delta)
        yield ("done", "".join(acc))
        return

    capped = r["courses"][:ANSWER_CAP]
    acc: list[str] = []
    for delta in answer.answer_stream(question, capped, _gen_facts(r["courses"], r["program_facts"]),
                                      topical=mode in ("semantic", "hybrid"), lang=lang):
        acc.append(delta)
        yield ("token", delta)
    full = answer.guard_citations("".join(acc), capped, lang)
    note = r.get("status_note") or ""           # "no midterm / no group" queries deterministically add the unknown hint
    if note:
        yield ("token", note)
    yield ("done", full + note)


def _gen_facts(courses: list[dict], program_facts):
    """Assemble extra facts fed to the answer model: truncate the program list, add a total for over-limit courses (answer reports the total from this)."""
    if isinstance(program_facts, list):                 # course_to_programs: truncate the program list
        return {"total_programs": len(program_facts), "programs": program_facts[:PROGRAM_CAP]} \
            if len(program_facts) > PROGRAM_CAP else program_facts
    pf = dict(program_facts) if isinstance(program_facts, dict) else {}
    if len(courses) > ANSWER_CAP:
        pf["total"] = len(courses)                       # neutral key; _infer_total/_TOTAL_KEYS recognizes it
    return pf or program_facts


def _print(res: dict):
    print(f"[mode={res['mode']}] {res['meta']}")
    plan = res["plan"] or {}
    if res["mode"] == "program" and plan.get("direction") == "course_to_programs":
        rows = res["program_facts"] or []
        print(f"命中 {len(rows)} 个 program" + ("(前15)" if len(rows) > 15 else "") + ":")
        for r in rows[:15]:
            via = f" (via {r['via_plan']})" if r.get("via_plan") else ""
            print(f"  [{r['requirement_type']}] {r['title']} — {r['course_list']}{via}")
    elif res["mode"] != "empty":
        rows = res["courses"]
        print(f"命中 {len(rows)} 门" + ("(前10)" if len(rows) > 10 else "") + ":")
        for c in rows[:10]:
            sim = f"  sim={c['sim']:.3f}" if c.get("sim") is not None else ""
            print(f"  {c.get('code')}  {c.get('title') or ''}  "
                  f"({c.get('semester') or '?'},{c.get('level') or '?'},exam={c.get('has_exam')}){sim}")
    if res["answer"] is not None:
        print(f"\n【回答】\n{res['answer']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--no-gen", action="store_true", help="只检索不生成回答")
    args = ap.parse_args()
    with psycopg.connect(DSN) as conn:
        retrieval.ensure_fts_index(conn)        # ensure the FTS index once (the read path no longer builds it)
        res = run(conn, args.question, generate=not args.no_gen)
    _print(res)


if __name__ == "__main__":
    main()
