"""i18n.py — answer language detection + bilingual building helpers (Chinese/English).

Purpose: answer in the same language the question was asked in. Language detection is a deterministic
classification (any CJK character means Chinese, otherwise English), with no LLM (global rule 12); the
sentence-by-sentence building of deterministic answers (compulsory/elective, ideographic comma vs comma,
choose-1-of-2 vs choose 1 of N, "N courses") branches by lang, instead of translating the Chinese answer —
this keeps a single source of truth for high-risk facts and zero mistranslation (student-facing red line 1).

Fixed sentences live in the MESSAGES registry, fetched via t(key, lang, **fmt); building with branching logic
stays in code and only calls this module's small helpers for the comma/label/quantifier parts.
"""
from __future__ import annotations
import re
from typing import Literal

Lang = Literal["zh", "en"]

_CJK_RE = re.compile(r"[一-鿿㐀-䶿]")


def detect_lang(text: str) -> Lang:
    """Question text with any CJK character -> zh, otherwise en (deterministic, no LLM).

    Edge case: one English sentence with a single Chinese course name still counts as zh. This is acceptable
    and recorded; add a ratio threshold only if the eval really shows a misjudgement.
    """
    return "zh" if _CJK_RE.search(text or "") else "en"


_CN_NUM = {2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九", 10: "十"}


def choice_word(k: int, lang: Lang) -> str:
    """Wording for k equivalent (interchangeable) courses: zh -> choose-1-of-2 / choose-1-of-many; en -> 'choose 1 of N'."""
    if lang == "en":
        return f"choose 1 of {k}"
    return f"{_CN_NUM.get(k, k)}选一"


def join_list(items, lang: Lang) -> str:
    """Join a list: zh uses the ideographic comma, en uses comma + space."""
    return ("、" if lang == "zh" else ", ").join(items)


def n_courses(n: int, lang: Lang) -> str:
    """Course count: zh -> 'N 门'; en -> 'N course' / 'N courses' (singular/plural)."""
    if lang == "en":
        return f"{n} course" if n == 1 else f"{n} courses"
    return f"{n} 门"


def n_programs(n: int, lang: Lang) -> str:
    """Program count: zh -> 'N 个'; en -> 'N program' / 'N programs'."""
    if lang == "en":
        return f"{n} program" if n == 1 else f"{n} programs"
    return f"{n} 个"


def label_req(req: str | None, lang: Lang) -> str:
    """Requirement type label: core/compulsory -> 必修/compulsory, elective -> 选修/elective, None -> empty string."""
    if req in ("core", "compulsory", "required"):
        return "必修" if lang == "zh" else "compulsory"
    if req in ("elective", "option", "optional"):
        return "选修" if lang == "zh" else "elective"
    return ""


# Registry of fixed sentences (whole sentences with no branching logic); building with logic does not go here,
# see each _ans_* building function.
# REVIEW: high-risk English (en) sentences must be human-checked line by line before going live (student-facing red line 6):
#   census / kb_refuse / kb_fallback_body directly affect a student's enrolment/fee decisions.
MESSAGES: dict[str, dict[Lang, str]] = {
    # course search returns no result (answer.py answer/answer_stream)
    "empty_answer": {
        "zh": "没有找到符合条件的课程。",
        "en": "No matching courses found.",
    },
    # course not found (answer.py course_detail)
    "course_not_found": {
        "zh": "未找到该课程,请检查课程码是否正确。",
        "en": "Course not found; please check the course code.",
    },
    # single-course fallback when there is no structured content (answer.py course_detail)
    "detail_see_card": {
        "zh": "{code} {title}。详细信息见下方课程卡与官方课程页。",
        "en": "{code} {title}. See the course card below and the official course page for details.",
    },
    # program not found (qa.py program route)
    "program_not_found": {
        "zh": "未找到名为「{name}」的专业,试试全称(如 Bachelor of Computer Science)。",
        "en": "No program named '{name}' was found. Try the full name (e.g. Bachelor of Computer Science).",
    },
    # question too broad (qa.py EMPTY_MSG)
    "empty_msg": {
        "zh": "问题太宽泛或无法形成检索条件,请补充学科方向或筛选条件(如学期 / 有无考试 / 专业)。",
        "en": "The question is too broad to form a search. Please add a subject area or filters "
              "(e.g. semester / with or without exam / program).",
    },
    # KB weak-recall refusal (red line 3: when unsure, give the official entry, do not guess)
    "kb_refuse": {
        "zh": ("抱歉,我在已收录的 UQ 官方页面里没找到能直接回答这个问题的内容。"
               "建议到 my.UQ 学生支持页查询:https://my.uq.edu.au/ "
               "(课程、专业、选课相关的问题也可以直接问我)。"),
        "en": ("Sorry, I could not find content in the indexed official UQ pages that directly "
               "answers this question. Please check the my.UQ student support page: "
               "https://my.uq.edu.au/ (you can also ask me about courses, programs, and enrolment)."),
    },
    # census date deterministic template (high-risk, red line 1: do not let the LLM free-style; do not hard-code the specific date that changes)
    "census": {
        "zh": ("census date(普查日)是每个学习周期(study period)最终确定你选课状态的截止日期:"
               "只有在此日期前完成的加退课才算数。你在 census date 当天的选课情况,决定该学期的费用责任"
               "(应缴学费与各项费用),也是办理 HECS-HELP / FEE-HELP / SA-HELP、提供税号(TFN)等事项的"
               "截止日。具体日期因课程和教学周期而异:请登录 mySI-net 的「Enrolments」查看每门课的 census "
               "date,或在学校 important dates 页查询。请以官方页面为准、注意时效。"),
        "en": ("The census date is the cut-off that finalises your enrolment status for each study "
               "period: only add/drop changes completed before this date count. Your enrolment as it "
               "stands on the census date determines your fee liability for that semester (tuition and "
               "charges payable), and is also the deadline for HECS-HELP / FEE-HELP / SA-HELP and for "
               "supplying your Tax File Number (TFN). The exact date varies by course and teaching "
               "period: log in to mySI-net under 'Enrolments' to see each course's census date, or "
               "check the university's important dates page. Rely on the official page and mind the timeliness."),
    },
    # KB sources block header (red line 2: every answer carries a clickable official link to verify)
    "kb_sources_header": {
        "zh": "\n\n来源(UQ 官方页面,可点击核对):\n",
        "en": "\n\nSources (official UQ pages, click to verify):\n",
    },
    # KB deterministic fallback when both tries return empty (red line 3: never leave a student with an empty answer)
    "kb_fallback_body": {
        "zh": "关于「{title}」,请查看下方 UQ 官方页面的说明(以官方页面为准,注意时效)。",
        "en": "For '{title}', please see the official UQ page below (rely on the official page; mind the timeliness).",
    },
}


def t(key: str, lang: Lang, **fmt) -> str:
    """Fetch a fixed sentence and pick by lang; **fmt fills str.format placeholders. A missing key raises directly, no silent fallback (rule 19)."""
    entry = MESSAGES[key]
    s = entry.get(lang) or entry["zh"]
    return s.format(**fmt) if fmt else s
