"""
answer.py — stage five: grounded answer generation
Feed the retrieved courses (+ optional program_facts) to the local qwen2.5-coder to produce a short Chinese answer.

Core constraints (all backed by the prompt + code guardrails together):
  - Answer only from the data passed in, each item supportable by the data, cite course codes, never invent courses or attributes
  - If courses is empty, return a fixed sentence directly, do not call the LLM
  - Control length (<= ~6 sentences or a bullet list), temperature 0

Division of work (matching "deterministic decisions in code, language tasks to the model"):
  - Code does the deterministic work: short-circuit empty results, serialize courses/program_facts into a "fact list", call fallbacks
  - The LLM only does the language work: organize the fact list into a natural Chinese answer

Usage:
    from app.services.answer import answer
    answer("有哪些机器学习的课", courses, program_facts=None) -> str
"""
from __future__ import annotations
import os
import re
import json
from collections.abc import Iterator

import requests

from app.services import llm
from app.core import i18n
from app.core.i18n import Lang

# Course code pattern: four uppercase letters + four digits (e.g. COMP4702), used for guardrail out-of-bounds checks
_COURSE_CODE_RE = re.compile(r"\b[A-Z]{4}\d{4}\b")

OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
LLM = os.environ.get("LLM_MODEL", "qwen2.5-coder:7b")

# Backward-compatible alias: eval scripts (relevance_scan) compare against answer.EMPTY_ANSWER (the zh string).
EMPTY_ANSWER = i18n.MESSAGES["empty_answer"]["zh"]


def is_empty_answer(text: str) -> bool:
    """Whether text is the 'no matching courses' empty answer in any supported language (lang-agnostic eval check)."""
    return text in i18n.MESSAGES["empty_answer"].values()


def is_kb_refuse(text: str) -> bool:
    """Whether text is the KB weak-recall refusal in any supported language (lang-agnostic eval/refuse check)."""
    return text in i18n.MESSAGES["kb_refuse"].values()

SYSTEM_ZH = """你是 UQ 选课助手。只能依据【事实】里给出的数据回答,用简洁中文。
硬性规则:
- 绝不编造课程、课程码或任何属性;凡是【事实】里没有的信息一律不提。
- 每提到一门课都要带上它的课程码(如 COMP4702)。
- 回答里每条信息都要能在【事实】中找到对应。
- 简短:不超过 6 句,或用要点列表;不要寒暄、不要重复问题、不要给学习建议。
- 若【事实】给出了命中总数且大于所列条数,必须在回答里说明「共 N 门,此处列出前 M 门」。
- 如果【事实】为空,只回答「没有找到符合条件的课程。」"""

SYSTEM_EN = """You are the UQ course assistant. Answer only from the data in [Facts], in clear English.
Hard rules:
- Never invent courses, course codes, or any attribute; never state anything not in [Facts].
- Every course you mention must carry its course code (e.g. COMP4702).
- Every piece of information in your answer must map to something in [Facts].
- Be short: at most 6 sentences, or a bullet list; no greetings, do not repeat the question, do not give study advice.
- If [Facts] gives a total count larger than the number listed, you must say "N in total, listing the first M here".
- If [Facts] is empty, answer exactly: No matching courses found."""

_SYSTEM = {"zh": SYSTEM_ZH, "en": SYSTEM_EN}

USER_TMPL_ZH = """问题:{q}

【事实】
{facts}

请只依据上面的【事实】用中文回答。"""

USER_TMPL_EN = """Question: {q}

[Facts]
{facts}

Answer in English based only on the [Facts] above."""

_USER_TMPL = {"zh": USER_TMPL_ZH, "en": USER_TMPL_EN}

# Topic relevance honesty (approach-3, folded into the same answer-generation call, no extra LLM call): the corpus may have no course
# about the topic at all, yet the bi-encoder still returns noise by semantic nearest-neighbor (e.g. "game development" recalls Gaming Cultures / a biology course), and the absolute
# sim cannot separate noise (game design 0.550) from a real course (statistics 0.556). Let the LLM judge whether the top recall really has a course about the
# topic (a classification task, rule 12), and when it is all noise, give an honest fallback statement instead of confidently listing it as "the X course" (red line: refuse over
# wrong). Soft fallback: still list the closest results, not a hard refuse. Only enabled for semantic/hybrid topic queries; a structured filter
# (e.g. "courses with no exam") has no topic, so it is not enabled, to avoid adding a needless statement.
_TOPIC_RELEVANCE_RULE = {
    "zh": ("\n- 相关性诚实:先判断【事实】里是否至少有一门课真正讲该问题主题。只要有一门真正相关,就"
           "正常作答——聚焦相关的课、忽略明显无关的噪声,不要因为列表里混了无关课就加声明。仅当没有"
           "任何一门真正讲该主题(全部只是课名/语义沾边的无关课)时,才在开头声明「未找到与『<主题>』"
           "强相关的课程,以下仅是语义最接近的结果,请自行甄别」再列出。"),
    "en": ("\n- Relevance honesty: first judge whether [Facts] has at least one course truly about the "
           "question's topic. If at least one is truly relevant, answer normally — focus on the relevant "
           "courses, ignore obvious noise, and do not add a disclaimer just because the list mixes in "
           "unrelated courses. Only when none truly covers the topic (all are mere name/semantic "
           "near-matches) should you open with \"No course strongly matching '<topic>' was found; the "
           "results below are only the closest semantic matches, please judge for yourself\", then list them."),
}


def _answer_system(topical: bool, lang: Lang = "zh") -> str:
    """When topical=True (a topic query), append the relevance-honesty instruction to the base SYSTEM, otherwise return it as is."""
    return _SYSTEM[lang] + _TOPIC_RELEVANCE_RULE[lang] if topical else _SYSTEM[lang]


# requirement_type label is bilingual via i18n.label_req (core/compulsory/required -> 必修/compulsory; elective/option/optional -> 选修/elective).


def _req_type_label(raw, lang: Lang = "zh") -> str | None:
    """Map the raw requirement_type value to a localized label; unknown values are returned as is, empty returns None."""
    if not raw:
        return None
    return i18n.label_req(str(raw).strip().lower(), lang) or str(raw).strip()


# _fmt_course field labels per language.
_FC_LABELS = {
    "zh": {"name": "名称", "level": "层次", "units": "学分", "semester": "学期", "campus": "校区",
           "exam_y": "有考试", "exam_n": "无考试", "hurdle_y": "有hurdle", "hurdle_n": "无hurdle",
           "group": "课程组", "none": "(本学期无开课信息)", "sep": ";", "list_join": "、"},
    "en": {"name": "name", "level": "level", "units": "units", "semester": "semester", "campus": "campus",
           "exam_y": "has exam", "exam_n": "no exam", "hurdle_y": "has hurdle", "hurdle_n": "no hurdle",
           "group": "course group", "none": "(no offering info this semester)", "sep": "; ", "list_join": ", "},
}


def _fmt_course(c: dict, lang: Lang = "zh") -> str:
    """Squash one course dict into a single human-readable fact line; only list fields that exist, to avoid giving the LLM attributes that do not exist.

    A program course has extra requirement_type (compulsory/elective) and course_list, which must be kept.
    A course with no title (not offered this semester) must still keep the course code + compulsory/elective, never produce an empty '- CODE:'.
    """
    L = _FC_LABELS[lang]
    code = c.get("code", "?")
    parts: list[str] = []
    if c.get("title"):
        parts.append(f"{L['name']}={c['title']}")
    req = _req_type_label(c.get("requirement_type"), lang)
    if req:
        parts.append(req)
    if c.get("level"):
        parts.append(f"{L['level']}={c['level']}")
    if c.get("units") is not None:
        parts.append(f"{L['units']}={c['units']}")
    if c.get("semester"):
        parts.append(f"{L['semester']}={c['semester']}")
    if c.get("location"):
        parts.append(f"{L['campus']}={c['location']}")
    if c.get("has_exam") is not None:
        parts.append(L["exam_y"] if c["has_exam"] else L["exam_n"])
    if c.get("has_hurdle") is not None:
        parts.append(L["hurdle_y"] if c["has_hurdle"] else L["hurdle_n"])
    if c.get("course_list"):
        # course_list may be a list or a string, normalize to comma-separated
        cl = c["course_list"]
        cl_str = L["list_join"].join(str(x) for x in cl) if isinstance(cl, (list, tuple)) else str(cl)
        parts.append(f"{L['group']}={cl_str}")
    if not parts:
        # no title and no attribute at all: mark "no offering info this semester", do not produce an empty line
        parts.append(L["none"])
    return f"- {code}:" + L["sep"].join(parts)


# Possible key names in program_facts that mean "total matches" (cover different upstream spellings)
_TOTAL_KEYS = ("命中总数", "total", "total_count", "count", "命中", "match_total")


def _infer_total(courses: list[dict], program_facts) -> int | None:
    """Infer the total matches from program_facts; if not found return None (the caller falls back to len(courses))."""
    if isinstance(program_facts, dict):
        for k in _TOTAL_KEYS:
            v = program_facts.get(k)
            if isinstance(v, int) and v >= 0:
                return v
    return None


def build_facts(courses: list[dict], program_facts=None, lang: Lang = "zh") -> str:
    """Serialize the retrieval results into the fact list fed to the LLM (deterministic, does not go through the model).

    The course heading line states the total matches to avoid silent truncation: the total prefers program_facts's "total matches",
    and falls back to len(courses) if absent. When the total > the number actually listed, explicitly write "N in total, listing M below".
    """
    lines: list[str] = []
    if courses:
        listed = len(courses)
        total = _infer_total(courses, program_facts)
        if total is None or total < listed:
            total = listed
        if lang == "en":
            lines.append(f"Courses ({total} in total, listing {listed} below):" if total > listed
                         else f"Courses ({total} in total):")
        else:
            lines.append(f"课程(共 {total} 门,以下列出 {listed} 门):" if total > listed
                         else f"课程(共 {total} 门):")
        lines += [_fmt_course(c, lang) for c in courses]
    if program_facts:
        # program_facts has an arbitrary structure, just dump it to JSON as supplementary facts for the LLM to use
        lines.append("补充事实(program_facts):" if lang == "zh" else "Additional facts (program_facts):")
        lines.append(json.dumps(program_facts, ensure_ascii=False, indent=None))
    return "\n".join(lines)


def guard_citations(text: str, courses: list[dict], lang: Lang = "zh") -> str:
    """Production guardrail: remove/flag out-of-bounds course codes cited in the answer (those not in the input courses code set).

    Process line by line: if a line has a course code outside the input, drop the whole line (to avoid leaving fictional-course info for the user).
    After dropping, append one warning line at the end listing the dropped out-of-bounds codes, so that "a skip always has a count and reason", not silent.
    A program name is not a course code (_COURSE_CODE_RE only matches 4 letters + 4 digits, so it will not hit a program name by mistake).
    """
    allowed = {c.get("code") for c in courses if c.get("code")}
    kept: list[str] = []
    dropped_codes: set[str] = set()
    for line in text.splitlines():
        extra = {m for m in _COURSE_CODE_RE.findall(line) if m not in allowed}
        if extra:
            dropped_codes |= extra
            continue
        kept.append(line)
    result = "\n".join(kept).strip()
    if dropped_codes:
        if lang == "en":
            warn = "[Warning] Removed out-of-bound (likely fabricated) course codes: " + ", ".join(sorted(dropped_codes))
        else:
            warn = f"[警告] 已剔除越界(疑似虚构)课程码:{'、'.join(sorted(dropped_codes))}"
        result = (result + "\n\n" + warn).strip() if result else warn
    return result


def answer(question: str, courses: list[dict], program_facts=None, topical: bool = False,
           lang: Lang = "zh") -> str:
    """Grounded answer generation: with no facts at all, use the fixed sentence, otherwise feed the fact list to qwen to produce a Chinese answer.
    topical=True (semantic/hybrid topic query) appends the relevance-honesty instruction: if the recall is all noise, fall back honestly, do not list it as the X course."""
    if not courses and not program_facts:
        return i18n.t("empty_answer", lang)

    facts = build_facts(courses, program_facts, lang)
    out = llm.call([
        {"role": "system", "content": _answer_system(topical, lang)},
        {"role": "user", "content": _USER_TMPL[lang].format(q=question, facts=facts)},
    ]).strip()
    # Production guardrail: out-of-bounds citation check (was only in __main__ before, now moved into the production path)
    return guard_citations(out, courses, lang)


def answer_stream(question: str, courses: list[dict], program_facts=None,
                  topical: bool = False, lang: Lang = "zh") -> Iterator[str]:
    """Streaming grounded generation: yield raw deltas token by token. With no facts at all, yield the fixed sentence.
    The guardrail guard_citations needs the full text, so the caller (qa.run_stream) applies it to the complete text at the end.
    topical means the same as in answer(): a topic query appends the relevance-honesty instruction."""
    if not courses and not program_facts:
        yield i18n.t("empty_answer", lang)
        return
    facts = build_facts(courses, program_facts, lang)
    yield from llm.call_stream([
        {"role": "system", "content": _answer_system(topical, lang)},
        {"role": "user", "content": _USER_TMPL[lang].format(q=question, facts=facts)},
    ])


# ---------- Knowledge base (FAQ / article) answer generation ----------
# Weak-recall refusal (no LLM call): when no relevant official content is retrieved, prefer to say unsure + give the official entry point (red line 3).
# Backward-compatible alias: eval scripts (answer_eval / llm_judge_eval) compare against answer.KB_REFUSE (the zh string).
KB_REFUSE = i18n.MESSAGES["kb_refuse"]["zh"]

# Reverse "always Chinese even for English questions" rule removed (full i18n): the answer follows the question language.
KB_SYSTEM_ZH = """你是 UQ 学生事务助手。只能依据【资料】(来自 UQ 官方页面的片段)回答,用简洁中文。
硬性规则:
- 【资料】已确认与问题相关,你必须依据它作答(英文资料转述成中文)。严禁回避、严禁输出
  「暂无信息」「无法回答」「资料未提及」之类的话;若资料只覆盖了问题的一部分,就回答能覆盖
  的部分,绝不因未完全覆盖而拒答。绝不编造步骤、数字、日期或网址。
- 涉及费用、截止日期、census date、退课/休学影响、考试安排等高风险信息,要提醒「以官方页面为准、注意时效」。
- 简短:不超过 6 句,或用要点列表;不寒暄、不重复问题。
- 不要自己写网址(系统会自动在末尾附上官方来源链接)。"""

KB_SYSTEM_EN = """You are the UQ student-support assistant. Answer only from [Material] (snippets from official UQ pages), in clear English.
Hard rules:
- [Material] is confirmed relevant; you must answer from it. Never evade, never output "no information" /
  "cannot answer" / "not mentioned in the material"; if the material covers only part of the question,
  answer the part it covers, and never refuse just because it is not fully covered. Never invent steps,
  numbers, dates, or URLs.
- For high-risk information (fees, deadlines, census date, withdrawal/leave impact, exam arrangements),
  remind the student to "rely on the official page and mind the timeliness".
- Be short: at most 6 sentences, or a bullet list; no greetings, do not repeat the question.
- Do not write URLs yourself (the system appends the official source links at the end)."""

_KB_SYSTEM = {"zh": KB_SYSTEM_ZH, "en": KB_SYSTEM_EN}

KB_USER_TMPL_ZH = """问题:{q}

【资料】(UQ 官方页面片段)
{facts}

请只依据上面的【资料】用中文回答;若问的是具体日期/时间,从资料的日期清单里找出对应日期直接作答。"""

KB_USER_TMPL_EN = """Question: {q}

[Material] (snippets from official UQ pages)
{facts}

Answer in English based only on the [Material] above; if the question asks for a specific date/time, find the matching date in the material's date list and answer directly."""

_KB_USER_TMPL = {"zh": KB_USER_TMPL_ZH, "en": KB_USER_TMPL_EN}


def _kb_facts(chunks: list[dict]) -> str:
    """Serialize KB chunks into a numbered fact list (chunk.text already has the "page title > h2 > h3" breadcrumb prefix)."""
    return "\n\n".join(f"[{i}] {(c.get('text') or '').strip()}"
                       for i, c in enumerate(chunks, 1))


def kb_sources_block(chunks: list[dict], lang: Lang = "zh") -> str:
    """List the official sources deduped by url (red line 2: every answer carries an official link verifiable in one click). Return an empty string if there are no sources."""
    seen: set = set()
    items: list[str] = []
    for c in chunks:
        u = c.get("url")
        if not u or u in seen:
            continue
        seen.add(u)
        title = c.get("page_title") or c.get("breadcrumb") or u
        sep = ":" if lang == "zh" else ": "
        items.append(f"- {title}{sep}{u}")
    return i18n.t("kb_sources_header", lang) + "\n".join(items) if items else ""


# High-risk high-frequency topic: use a deterministic answer, do not let the LLM free-wheel (student-facing red line 1).
# Triggered by the recalled top chunk's page title (a stable identifier), not by fragile query keywords; do not hardcode a specific date that changes.
# The bilingual template lives in i18n.MESSAGES["census"] (high-risk EN wording — human-review gate, red line 6).


def fixed_kb_body(chunks: list[dict], lang: Lang = "zh") -> str | None:
    """Deterministic answer body for high-risk topics (currently: census date); return the template on a hit, otherwise None.

    Triggered by the recalled top chunk's page_title (stable), with content faithful to the official page and no hardcoded specific date that changes (red line 1).
    Shared by the streaming / non-streaming / finishing paths, so high-frequency high-risk questions like census get a 100% consistent, verifiable answer."""
    if not chunks:
        return None
    if "census date" in (chunks[0].get("page_title") or "").lower():
        return i18n.t("census", lang)
    return None


# LLM empty-answer markers: chunks already passed the answerability gate (material confirmed relevant), yet these appearing in the body = an abnormal empty answer, must not reach the student.
# Both languages are checked regardless of question lang, so an English "no information" answer is also caught and retried.
_KB_EMPTY_MARKERS = ("暂无信息", "无法回答", "无法回应", "资料未提及", "未提及相关",
                     "无相关", "没有相关信息", "暂无相关", "暂时无法", "无可用信息",
                     "no information", "cannot answer", "can't answer", "unable to answer",
                     "not mentioned in the material", "no relevant information")

KB_RETRY_HINT = {
    "zh": ("\n\n注意:以上【资料】已确认与该问题直接相关,请务必基于它用简洁中文给出"
           "实质回答,严禁回答「暂无信息」之类的话。"),
    "en": ("\n\nNote: the [Material] above is confirmed directly relevant to this question. You must "
           "give a substantive answer in English based on it; never answer \"no information\" or similar."),
}


def is_empty_kb_answer(body: str) -> bool:
    """Judge whether the KB body is an abnormal empty answer (too short or hitting an empty-answer marker); strip prefixes like "- " "[1]" before checking the real length.
    The streaming finish (qa.run_stream) also uses it for its fallback check, so it is public."""
    stripped = body.strip().lstrip("-[]() .0123456789").strip()
    if len(stripped) < 8:
        return True
    low = body.lower()
    return any(m in low for m in _KB_EMPTY_MARKERS)


def _gen_kb_body(question: str, chunks: list[dict], *, retry: bool = False, lang: Lang = "zh") -> str:
    """Call the LLM to generate the KB body; when retry=True, strengthen the instruction + raise temperature, to escape DeepSeek's tendency to empty-answer some questions."""
    user = _KB_USER_TMPL[lang].format(q=question, facts=_kb_facts(chunks))
    if retry:
        user += KB_RETRY_HINT[lang]
    return llm.call([
        {"role": "system", "content": _KB_SYSTEM[lang]},
        {"role": "user", "content": user},
    ], temperature=(0.4 if retry else 0.0)).strip()


def _kb_fallback_body(chunks: list[dict], lang: Lang = "zh") -> str:
    """Deterministic downgrade: when the LLM empty-answers both times, give a deterministic pointer to the official page (never leave an empty answer for the student, red line 3)."""
    default_title = "你的问题" if lang == "zh" else "your question"
    title = chunks[0].get("page_title") or chunks[0].get("breadcrumb") or default_title
    return i18n.t("kb_fallback_body", lang, title=title)


def kb_answer_body(question: str, chunks: list[dict], lang: Lang = "zh") -> str:
    """KB body (without the source block): generate -> retry on empty answer (stronger instruction + higher temperature) -> deterministic downgrade if still empty.

    Non-empty chunks = already passed the answerability gate (material confirmed relevant), so an empty LLM answer is abnormal (DeepSeek t=0 sampling jitter).
    Streaming (qa.run_stream finish) and non-streaming share this function, so both fallbacks match and the student never sees "no info available"."""
    fixed = fixed_kb_body(chunks, lang=lang)
    if fixed:
        return fixed
    body = _gen_kb_body(question, chunks, lang=lang)
    if is_empty_kb_answer(body):
        body = _gen_kb_body(question, chunks, retry=True, lang=lang)
    if is_empty_kb_answer(body):
        body = _kb_fallback_body(chunks, lang=lang)
    return body


def answer_kb(question: str, chunks: list[dict], lang: Lang = "zh") -> str:
    """Grounded answer based on KB chunks: with no chunk use the refusal sentence; otherwise body (with fallback) + code deterministically appends official sources."""
    if not chunks:
        return i18n.t("kb_refuse", lang)
    return kb_answer_body(question, chunks, lang=lang) + kb_sources_block(chunks, lang=lang)


def answer_kb_stream(question: str, chunks: list[dict], lang: Lang = "zh") -> Iterator[str]:
    """Streaming KB answer: with no chunk yield the refusal sentence; otherwise stream the body token by token.
    The source block is appended by the caller (qa.run_stream) at the end, to guarantee 100% an official link."""
    if not chunks:
        yield i18n.t("kb_refuse", lang)
        return
    yield from llm.call_stream([
        {"role": "system", "content": _KB_SYSTEM[lang]},
        {"role": "user", "content": _KB_USER_TMPL[lang].format(q=question, facts=_kb_facts(chunks))},
    ])


# ---------- Single-course detail answer (grounded in the official syllabus, fallback for any single-course question) ----------
# Clearly-stated high-risk/precise questions (prerequisites, assessment weight, whether there is a certain assessment type...) are already intercepted by the front deterministic gate into structured answers;
# here we only fall back on long-tail questions (what it covers / who it suits / how hard / whether a field is mentioned...), all grounded in the full record fed in.
COURSE_DETAIL_SYSTEM_ZH = """你是 UQ 选课助手。只依据【课程资料】(UQ 官方课程大纲)用简洁中文回答学生关于这门课的问题。
硬性规则:
- 只用【课程资料】里的信息,英文转述成中文;资料没覆盖的就直说「资料未提供」,绝不编造或猜测。
- 高风险/精确信息(先修要求、精确权重或分数、学分、日期):只能照搬资料原文,绝不改写其 and/or 逻辑或数字。
- 问「有没有某类考核」时据【考核项】如实回答有/无;但不要逐条罗列全部考核或复述精确权重(考核明细另行结构化展示)。
- 介绍类问题(讲什么/核心主题/适合谁修)按资料信息量自适应展开,不注水、不重复;不寒暄、不写网址、不重复课程码。"""

COURSE_DETAIL_SYSTEM_EN = """You are the UQ course assistant. Answer the student's question about this course in clear English, based only on [Course material] (the official UQ course profile).
Hard rules:
- Use only the information in [Course material]; render it faithfully. If something is not covered, say "not provided in the material"; never invent or guess.
- High-risk/precise information (prerequisites, exact weights or marks, units, dates): copy it verbatim from the material; never change its and/or logic or numbers.
- When asked "is there a certain assessment type", answer yes/no honestly from [Assessment items]; but do not list every assessment item or restate exact weights (the breakdown is shown separately, structured).
- For introductory questions (what it covers / key topics / who it suits), expand adaptively to the information available; no padding, no repetition; no greetings, no URLs, do not repeat the course code."""

_COURSE_DETAIL_SYSTEM = {"zh": COURSE_DETAIL_SYSTEM_ZH, "en": COURSE_DETAIL_SYSTEM_EN}

COURSE_DETAIL_USER_ZH = """课程:{code} {title}
学生的问题:{question}

【课程资料】
{facts}

请只依据上面的【课程资料】用中文回答学生的问题;资料未覆盖的部分明确说明,不要编造。"""

COURSE_DETAIL_USER_EN = """Course: {code} {title}
Student's question: {question}

[Course material]
{facts}

Answer the student's question in English based only on the [Course material] above; clearly state what the material does not cover, do not invent."""

_COURSE_DETAIL_USER = {"zh": COURSE_DETAIL_USER_ZH, "en": COURSE_DETAIL_USER_EN}


def _assessments_for_llm(course: dict, lang: Lang = "zh") -> str:
    """Serialize assessment items for the LLM (with category, to judge "is there a certain assessment type"); return an empty string if no data."""
    items = course.get("assessments")
    if not isinstance(items, list):
        return ""
    cat_label = "类别" if lang == "zh" else "category"
    rows: list[str] = []
    for a in items:
        if not isinstance(a, dict):
            continue
        task = str(a.get("task") or "").strip() or ("考核项" if lang == "zh" else "assessment item")
        bits: list[str] = []
        if a.get("category"):
            bits.append(f"{cat_label} {a['category']}")
        if a.get("weight") is not None:
            bits.append(f"{_fmt_num(a['weight'])}%")
        if a.get("hurdle"):
            bits.append("hurdle")
        rows.append(task + (f"[{','.join(bits)}]" if bits else ""))
    return ";".join(rows)


# Per-language labels for the course-detail facts block fed to the LLM.
_CDF_LABELS = {
    "zh": {"desc": "课程简介:", "topics": "主题:", "lo": "学习成果:", "asmt": "考核项:",
           "prereq": "先修要求(原文):", "prereq_none": "先修要求:无", "units": "学分:",
           "sems": "开课学期:", "locs": "校区:", "join": "、"},
    "en": {"desc": "Description: ", "topics": "Topics: ", "lo": "Learning outcomes: ", "asmt": "Assessment: ",
           "prereq": "Prerequisites (verbatim): ", "prereq_none": "Prerequisites: none", "units": "Units: ",
           "sems": "Offered: ", "locs": "Campus: ", "join": ", "},
}


def _course_detail_facts(course: dict, lang: Lang = "zh") -> str:
    """Serialize the full structured single-course record for the grounded LLM fallback Q&A.
    High-risk fields (prerequisite original text / precise weight) are given with the record, and the prompt requires copying them verbatim without rewriting;
    clearly-stated high-risk questions are already intercepted by the front deterministic gate before the LLM."""
    L = _CDF_LABELS[lang]
    parts: list[str] = []
    if course.get("description"):
        parts.append(L["desc"] + str(course["description"]))
    if course.get("topics"):
        parts.append(L["topics"] + str(course["topics"])[:600])
    if course.get("learning_outcomes"):
        parts.append(L["lo"] + str(course["learning_outcomes"])[:600])
    asmt = _assessments_for_llm(course, lang)
    if asmt:
        parts.append(L["asmt"] + asmt)
    raw = (course.get("prerequisite_raw") or "").strip()
    parts.append(L["prereq"] + raw if raw else L["prereq_none"])
    if course.get("units") is not None:
        parts.append(f"{L['units']}{_fmt_num(course['units'])}")
    sems = [s for s in (course.get("semesters") or []) if s]
    if sems:
        parts.append(L["sems"] + L["join"].join(sems))
    locs = [l for l in (course.get("locations") or []) if l]
    if locs:
        parts.append(L["locs"] + L["join"].join(locs))
    return "\n\n".join(parts)


def _has_detail_content(course: dict) -> bool:
    """Whether there is real structured content for the LLM fallback to answer from; when all empty, use the fallback text without calling the LLM."""
    return bool(course.get("description") or course.get("topics")
                or course.get("learning_outcomes") or _has_assessments(course)
                or (course.get("prerequisite_raw") or "").strip())


# ---------- Single-course structured sub-questions (prerequisite/assessment/units/offering): answered deterministically, not given to the LLM ----------
# High-cost facts (the prerequisite and/or logic, assessment weight numbers, units, offering) get a precise response from structured fields (red line 1),
# without letting the LLM paraphrase freely so the logic/numbers are not changed; intent detection runs in code (rule 12), and only on no match does it fall back to the LLM general intro.
_DETAIL_INTENT_KW: dict[str, tuple[str, ...]] = {
    "prereq": ("先修", "先决", "前置", "前导", "修读要求", "prerequisite", "prereq"),
    "assessment": ("考核", "考评", "评估", "评分", "成绩构成", "怎么考", "如何考",
                   "考试", "占比", "assessment"),
    "units": ("学分", "几分", "多少分", "units"),
    "semester": ("开课", "哪个学期", "什么时候开", "第几学期", "何时开", "开设", "semester"),
}


def _detail_intents(question: str) -> list[str]:
    """The matched sub-question intent keys (deterministic keywords), returned in the fixed order of _DETAIL_INTENT_KW; return an empty list on no match."""
    q = (question or "").lower()
    return [key for key, kws in _DETAIL_INTENT_KW.items()
            if any(kw.lower() in q for kw in kws)]


def _fmt_num(x) -> str:
    """2.0 -> '2', 22.5 -> '22.5' (drop the .0 of an integer, keep a real fraction)."""
    f = float(x)
    return str(int(f)) if f == int(f) else str(f)


def _detail_prereq(course: dict, lang: Lang = "zh") -> str:
    code = course.get("code", "?")
    title = course.get("title") or ""
    raw = (course.get("prerequisite_raw") or "").strip().rstrip("。.")
    if lang == "en":
        if not raw:
            return f"{code} ({title}) has no prerequisites."
        return f"{code} prerequisites: {raw}. Refer to the official course profile (ECP)."
    if not raw:
        return f"{code}({title})没有先修课要求。"
    return f"{code} 的先修课要求:{raw}。以官方课程页(ECP)为准。"


def _fmt_assessment_item(a: dict, lang: Lang = "zh") -> str:
    """A single assessment item -> 'task(weight%, hurdle)'; with no weight/hurdle, output only task."""
    task = str(a.get("task") or "").strip() or ("考核项" if lang == "zh" else "assessment item")
    extra: list[str] = []
    if a.get("weight") is not None:
        extra.append(f"{_fmt_num(a['weight'])}%")
    if a.get("hurdle"):
        extra.append("hurdle")
    if not extra:
        return task
    return task + (f"({'、'.join(extra)})" if lang == "zh" else f" ({', '.join(extra)})")


def _detail_assessment(course: dict, lang: Lang = "zh") -> str:
    code = course.get("code", "?")
    items = course.get("assessments")
    parts = ([_fmt_assessment_item(a, lang) for a in items if isinstance(a, dict)]
             if isinstance(items, list) else [])
    if lang == "en":
        if not parts:
            return f"{code} has no structured assessment info; please see the official course profile (ECP)."
        return f"{code} assessment breakdown: " + ", ".join(parts) + ". Refer to the official course page."
    if not parts:
        return f"{code} 暂无结构化考核信息,请查看官方课程页(ECP)。"
    return f"{code} 的考核组成:" + "、".join(parts) + "。以官方课程页为准。"


def _detail_units(course: dict, lang: Lang = "zh") -> str:
    code = course.get("code", "?")
    title = course.get("title") or ""
    u = course.get("units")
    if lang == "en":
        if u is None:
            return f"{code} has no units info; please see the official course page."
        return f"{code} ({title}) is {_fmt_num(u)} units."
    if u is None:
        return f"{code} 暂无学分信息,请查看官方课程页。"
    return f"{code}({title})是 {_fmt_num(u)} 学分。"


def _detail_semester(course: dict, lang: Lang = "zh") -> str:
    code = course.get("code", "?")
    sems = [s for s in (course.get("semesters") or []) if s]
    locs = [l for l in (course.get("locations") or []) if l]
    if lang == "en":
        if not sems:
            return f"{code} has no offering-semester info; please see the official course page."
        base = f"{code} offered in: {', '.join(sems)}"
        if locs:
            base += f"; campus: {', '.join(locs)}"
        return base + ". Refer to the official course page."
    if not sems:
        return f"{code} 暂无开课学期信息,请查看官方课程页。"
    base = f"{code} 开设学期:{'、'.join(sems)}"
    if locs:
        base += f";校区:{'、'.join(locs)}"
    return base + "。以官方课程页为准。"


_DETAIL_FMT = {
    "prereq": _detail_prereq,
    "assessment": _detail_assessment,
    "units": _detail_units,
    "semester": _detail_semester,
}


# Low-ambiguity assessment-type lookup table (for single-course "is there an X assessment"): type -> (Chinese label, keywords).
# The keywords are used both to "identify which type is being asked" (Chinese/English) and to "match the assessment task/category" (data is mostly English).
# Only plainly-named types go in the table (adding a new type = adding a line); high-ambiguity dimensions like exam/midterm/group use a dedicated three-state classifier, not here.
_ASSESSMENT_TYPES: dict[str, tuple[tuple[str, str], tuple[str, ...]]] = {
    "presentation": (("演讲/展示", "presentation/demonstration"), ("presentation", "demonstration", "演讲", "展示", "汇报")),
    "quiz": (("测验", "quiz"), ("quiz", "测验", "小测")),
    "essay": (("论文", "essay"), ("essay", "论文", "小论文")),
    "report": (("报告", "report"), ("report", "报告")),
    "project": (("项目", "project"), ("project", "项目")),
    "poster": (("海报", "poster"), ("poster", "海报")),
    "portfolio": (("作品集", "portfolio"), ("portfolio", "作品集")),
    "participation": (("课堂参与", "class participation"), ("participation", "参与", "出勤")),
    "reflection": (("反思", "reflection"), ("reflection", "reflective", "反思")),
}


def _assessment_label(t: str, lang: Lang) -> str:
    """The localized label for an assessment type key (zh/en)."""
    zh, en = _ASSESSMENT_TYPES[t][0]
    return zh if lang == "zh" else en


def _matched_assessment_type(question: str) -> str | None:
    """The assessment-type key the question matches (in table order); return None on no match."""
    q = (question or "").lower()
    for t, (_labels, kws) in _ASSESSMENT_TYPES.items():
        if any(k.lower() in q for k in kws):
            return t
    return None


def _assessment_type_answer(question: str, course: dict, lang: Lang = "zh") -> str | None:
    """Single-course "is there a <certain type> assessment" -> answer yes/no + matched items deterministically from assessments; return None if the question names no type.

    Only with assessment data is a "yes/no" conclusion drawn; with no assessments data it is treated as unknown (refuse over wrong), never silently treated as "no".
    """
    t = _matched_assessment_type(question)
    if t is None:
        return None
    label = _assessment_label(t, lang)
    kws = _ASSESSMENT_TYPES[t][1]
    code = course.get("code", "?")
    items = course.get("assessments")
    if not isinstance(items, list) or not any(isinstance(a, dict) for a in items):
        if lang == "en":
            return (f"{code} has no structured assessment info, so whether it includes {label} assessment "
                    f"cannot be confirmed; please see the official course profile (ECP).")
        return f"{code} 暂无结构化考核信息,无法确认是否有{label}考核,请查看官方课程页(ECP)。"
    kws_l = tuple(k.lower() for k in kws)
    matched = [
        a for a in items if isinstance(a, dict)
        and any(k in ((a.get("task") or "") + " " + (a.get("category") or "")).lower() for k in kws_l)
    ]
    if lang == "en":
        if not matched:
            return f"{code} has no {label} assessment. Refer to the official course page."
        return f"{code} has {label} assessment: " + ", ".join(_fmt_assessment_item(a, lang) for a in matched) + ". Refer to the official course page."
    if not matched:
        return f"{code} 没有{label}类考核。以官方课程页为准。"
    return f"{code} 有{label}类考核:" + "、".join(_fmt_assessment_item(a, lang) for a in matched) + "。以官方课程页为准。"


def detail_structured_answer(question: str, course: dict, lang: Lang = "zh") -> str | None:
    """On matching a prerequisite/assessment/units/offering sub-question -> answer deterministically from structured fields (one paragraph per item); return None on no match.
    First judge "is there a certain assessment type" (more specific), then judge the general sub-questions."""
    typed = _assessment_type_answer(question, course, lang)
    if typed is not None:
        return typed
    intents = _detail_intents(question)
    if not intents:
        return None
    return "\n\n".join(_DETAIL_FMT[i](course, lang) for i in intents)


def _has_assessments(course: dict) -> bool:
    """Whether there are renderable structured assessment items (any dict with a non-empty task)."""
    items = course.get("assessments")
    return isinstance(items, list) and any(
        isinstance(a, dict) and str(a.get("task") or "").strip() for a in items)


def _with_assessment_appendix(intro: str, course: dict, lang: Lang = "zh") -> str:
    """After the general intro, append the deterministic assessment makeup (only when there is data); assessment still comes from structured fields, not the LLM."""
    if not _has_assessments(course):
        return intro
    return intro + "\n\n" + _detail_assessment(course, lang)


def _detail_struct_context(course: dict, intents: list[str]) -> list[str]:
    """The fact basis for the deterministic sub-question answer (for llm_judge faithfulness, taken from the same set of fields as the answer)."""
    out: list[str] = []
    if "prereq" in intents:
        out.append("prerequisite_raw=" + ((course.get("prerequisite_raw") or "").strip() or "(空=无先修)"))
    if "assessment" in intents:
        out.append("assessments=" + json.dumps(course.get("assessments") or [], ensure_ascii=False))
    if "units" in intents:
        out.append(f"units={course.get('units')}")
    if "semester" in intents:
        out.append("semesters=" + json.dumps(course.get("semesters") or [], ensure_ascii=False)
                   + " locations=" + json.dumps(course.get("locations") or [], ensure_ascii=False))
    return out


def answer_course_detail(question: str, course: dict | None, lang: Lang = "zh") -> str:
    """Single-course Q&A: on matching a prerequisite/assessment/units/offering sub-question use a structured deterministic answer (red line 1), otherwise an LLM grounded intro.
    The structured facts are also shown by the frontend detail card; for a general "what it covers" with no sub-question match, the LLM generates a Chinese intro."""
    if not course:
        return i18n.t("course_not_found", lang)
    structured = detail_structured_answer(question, course, lang)
    if structured is not None:
        return structured
    if not _has_detail_content(course):
        intro = i18n.t("detail_see_card", lang, code=course["code"], title=course.get("title") or "")
    else:
        intro = llm.call([
            {"role": "system", "content": _COURSE_DETAIL_SYSTEM[lang]},
            {"role": "user", "content": _COURSE_DETAIL_USER[lang].format(
                code=course["code"], title=course.get("title") or "",
                question=question, facts=_course_detail_facts(course, lang))},
        ]).strip()
    return _with_assessment_appendix(intro, course, lang)


def answer_course_detail_stream(question: str, course: dict | None, lang: Lang = "zh") -> Iterator[str]:
    """Streaming version of single-course Q&A: on a sub-question match yield the structured deterministic answer (one block, no LLM); otherwise stream the intro token by token."""
    if not course:
        yield i18n.t("course_not_found", lang)
        return
    structured = detail_structured_answer(question, course, lang)
    if structured is not None:
        yield structured
        return
    if not _has_detail_content(course):
        yield i18n.t("detail_see_card", lang, code=course["code"], title=course.get("title") or "")
    else:
        yield from llm.call_stream([
            {"role": "system", "content": _COURSE_DETAIL_SYSTEM[lang]},
            {"role": "user", "content": _COURSE_DETAIL_USER[lang].format(
                code=course["code"], title=course.get("title") or "",
                question=question, facts=_course_detail_facts(course, lang))},
        ])
    if _has_assessments(course):
        yield "\n\n" + _detail_assessment(course, lang)


def gen_contexts(mode: str, courses: list[dict] | None = None, program_facts=None,
                 chunks: list[dict] | None = None, course: dict | None = None,
                 question: str | None = None, lang: Lang = "zh") -> list[str]:
    """Return, item by item, the retrieval context actually fed to the LLM for each mode (for eval/debug, same source as production generation, zero drift).

    Reuses production serialization (_fmt_course / _kb_facts same source / _course_detail_facts), each item maps to RAGAS's
    retrieved_contexts; modes with no LLM context such as program/empty return an empty list.
    When course_detail matches a sub-question, return the structured-field basis (the answer is deterministic, not LLM), same as program.
    """
    if mode == "kb":
        return [(c.get("text") or "").strip() for c in (chunks or []) if c.get("text")]
    if mode == "course_detail":
        intents = _detail_intents(question or "")
        if intents:
            return _detail_struct_context(course or {}, intents)
        facts = _course_detail_facts(course or {}, lang)
        return [facts] if facts.strip() else []
    items = [_fmt_course(c, lang) for c in (courses or [])]
    if program_facts:
        head = "补充事实(program_facts):" if lang == "zh" else "Additional facts (program_facts):"
        items.append(head + json.dumps(program_facts, ensure_ascii=False))
    return items


if __name__ == "__main__":
    # ---- Deterministic self-test (does not depend on Ollama, covers three fix cases) ----
    print("=== 确定性自测 ===")

    # Reproduce case A: a program course with no title, no empty line and marked "compulsory"
    line = _fmt_course({"code": "COMP1200", "requirement_type": "core"})
    print("A 无title课:", line)
    assert line == "- COMP1200:必修", line
    assert line != "- COMP1200:", "不能产出空事实行"

    # course_list + elective must also be kept
    line2 = _fmt_course({"code": "COMP9999", "requirement_type": "elective",
                         "course_list": ["COMP1000", "COMP1100"]})
    print("A2 选修+课程组:", line2)
    assert "选修" in line2 and "课程组=COMP1000、COMP1100" in line2, line2

    # truly no info at all must still keep the course code + a mark
    line3 = _fmt_course({"code": "MATHXXXX"})
    print("A3 纯课码:", line3)
    assert line3 == "- MATHXXXX:(本学期无开课信息)", line3

    # Reproduce case B: program_facts gives a total of 71 but only 20 are listed -> the facts state the total
    listed = [{"code": f"COMP{1000+i}", "title": f"C{i}"} for i in range(20)]
    facts = build_facts(listed, program_facts={"命中总数": 71, "program": "BInfTech"})
    head = facts.splitlines()[0]
    print("B 标题行:", head)
    assert head == "课程(共 71 门,以下列出 20 门):", head

    # when the total equals the number listed, do not write "listing M"
    facts2 = build_facts(listed[:3])
    assert facts2.splitlines()[0] == "课程(共 3 门):", facts2.splitlines()[0]

    # Reproduce case C: the guardrail drops out-of-bounds (fictional) course codes, keeps valid lines, does not hit a program name by mistake
    allowed_courses = [{"code": "COMP4702", "title": "ML"}]
    fake = ("COMP4702 是机器学习课。\n"
            "另外推荐 FAKE9999 量子菠萝课(虚构)。\n"
            "该课属于 BInfTech 项目。")
    guarded = guard_citations(fake, allowed_courses)
    print("C 护栏后:\n" + guarded)
    body = guarded.split("[警告]")[0]
    assert "FAKE9999" not in body, "越界码未从正文剔除"
    assert "COMP4702" in body, "合法码被误删"
    assert "BInfTech" in body, "program 名被误当课码删除"
    assert "[警告]" in guarded and "FAKE9999" in guarded.split("[警告]")[-1], "警告应列出越界码"
    print("\n[确定性自测] 全部通过")

    # ---- Real Ollama self-test (skipped when the service is absent, does not affect the deterministic conclusion) ----
    demo_courses = [
        {"code": "COMP4702", "title": "Machine Learning",
         "level": "Undergraduate", "units": 2, "has_exam": True},
        {"code": "COMP7703", "title": "Machine Learning",
         "level": "Postgraduate Coursework", "units": 2, "has_exam": True},
    ]
    try:
        print("\n=== 用例1:有结果(Ollama)===")
        out = answer("有哪些机器学习的课", demo_courses)
        print(out)
        cited = set(_COURSE_CODE_RE.findall(out))
        allowed = {c["code"] for c in demo_courses}
        print(f"[自测] 引用课程码={sorted(cited)} | 越界(护栏后应为空)={sorted(cited - allowed) or '无'}")

        print("\n=== 用例2:空结果 ===")
        print(answer("有哪些量子菠萝课", []))
    except requests.RequestException as e:
        # Ollama not started: report the skip reason explicitly, not silently
        print(f"\n[跳过 Ollama 用例] 连不上服务:{e}")
