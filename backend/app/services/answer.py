"""
answer.py — 阶段五:grounded 答案生成
把检索到的 courses(+ 可选 program_facts)喂本地 qwen2.5-coder,生成简洁中文回答。

核心约束(全部靠提示词 + 代码护栏一起兜底):
  - 只依据传入数据作答,逐项可由数据支撑,引用课程码,绝不编造课程或属性
  - courses 为空直接返回固定句,不调用 LLM
  - 控制长度(≤ ~6 句或要点列表),temperature 0

分工(对应「确定性决策用代码,语言任务交模型」):
  - 代码做确定性活:空结果短路、把 courses/program_facts 序列化成「事实清单」、调用兜底
  - LLM 只做语言活:把事实清单组织成自然中文回答

用法:
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

# 课程码模式:四个大写字母 + 四位数字(如 COMP4702),用于护栏越界校验
_COURSE_CODE_RE = re.compile(r"\b[A-Z]{4}\d{4}\b")

OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
LLM = os.environ.get("LLM_MODEL", "qwen2.5-coder:7b")

EMPTY_ANSWER = "没有找到符合条件的课程。"

SYSTEM = """你是 UQ 选课助手。只能依据【事实】里给出的数据回答,用简洁中文。
硬性规则:
- 绝不编造课程、课程码或任何属性;凡是【事实】里没有的信息一律不提。
- 每提到一门课都要带上它的课程码(如 COMP4702)。
- 回答里每条信息都要能在【事实】中找到对应。
- 简短:不超过 6 句,或用要点列表;不要寒暄、不要重复问题、不要给学习建议。
- 若【事实】给出了命中总数且大于所列条数,必须在回答里说明「共 N 门,此处列出前 M 门」。
- 如果【事实】为空,只回答「没有找到符合条件的课程。」"""

USER_TMPL = """问题:{q}

【事实】
{facts}

请只依据上面的【事实】用中文回答。"""

# 主题相关性诚实(approach-3,折叠进答案生成同一次调用,不加 LLM 调用):语料可能根本没有讲该
# 主题的课,bi-encoder 仍按语义近邻返回噪声(如「游戏开发」召回 Gaming Cultures/生物课),且绝对
# sim 无法把噪声(game design 0.550)和真课(statistics 0.556)分开。让 LLM 判 top 召回是否真有讲该
# 主题的课(分类活,规则12),全是噪声时给诚实兜底声明而非自信当成「X 课」列出(红线:refuse over
# wrong)。软兜底:仍列出最接近结果,非硬拒。仅 semantic/hybrid 主题查询启用;结构化 filter
# (如「没有考试的课」)无主题,不启用以免无端添加声明。
_TOPIC_RELEVANCE_RULE = (
    "\n- 相关性诚实:先判断【事实】里是否至少有一门课真正讲该问题主题。只要有一门真正相关,就"
    "正常作答——聚焦相关的课、忽略明显无关的噪声,不要因为列表里混了无关课就加声明。仅当没有"
    "任何一门真正讲该主题(全部只是课名/语义沾边的无关课)时,才在开头声明「未找到与『<主题>』"
    "强相关的课程,以下仅是语义最接近的结果,请自行甄别」再列出。")


def _answer_system(topical: bool) -> str:
    """topical=True(主题查询)时在基础 SYSTEM 上追加相关性诚实指令,否则原样返回。"""
    return SYSTEM + _TOPIC_RELEVANCE_RULE if topical else SYSTEM


# requirement_type 白名单:program_course 里的取值 -> 中文标签(确定性映射,不交给模型)
_REQ_TYPE_LABEL = {
    "core": "必修",
    "compulsory": "必修",
    "required": "必修",
    "elective": "选修",
    "option": "选修",
    "optional": "选修",
}


def _req_type_label(raw) -> str | None:
    """把 requirement_type 原始值映射成中文标签;未知值原样返回,空值返回 None。"""
    if not raw:
        return None
    return _REQ_TYPE_LABEL.get(str(raw).strip().lower(), str(raw).strip())


def _fmt_course(c: dict) -> str:
    """把一门课的 dict 压成一行人类可读事实;只列存在的字段,避免给 LLM 不存在的属性。

    program 课多了 requirement_type(必修/选修)和 course_list,必须保留。
    无 title 的课(本学期未开课)也要保住课码 + 必修/选修,绝不产出空的 '- CODE:'。
    """
    code = c.get("code", "?")
    parts: list[str] = []
    if c.get("title"):
        parts.append(f"名称={c['title']}")
    req = _req_type_label(c.get("requirement_type"))
    if req:
        parts.append(req)
    if c.get("level"):
        parts.append(f"层次={c['level']}")
    if c.get("units") is not None:
        parts.append(f"学分={c['units']}")
    if c.get("semester"):
        parts.append(f"学期={c['semester']}")
    if c.get("location"):
        parts.append(f"校区={c['location']}")
    if c.get("has_exam") is not None:
        parts.append("有考试" if c["has_exam"] else "无考试")
    if c.get("has_hurdle") is not None:
        parts.append("有hurdle" if c["has_hurdle"] else "无hurdle")
    if c.get("course_list"):
        # course_list 可能是列表或字符串,统一成逗号分隔
        cl = c["course_list"]
        cl_str = "、".join(str(x) for x in cl) if isinstance(cl, (list, tuple)) else str(cl)
        parts.append(f"课程组={cl_str}")
    if not parts:
        # 没有 title 也没有任何属性:标注本学期无开课信息,不产空行
        parts.append("(本学期无开课信息)")
    return f"- {code}:" + ";".join(parts)


# program_facts 里表示「命中总数」的可能键名(不同上游写法都兜住)
_TOTAL_KEYS = ("命中总数", "total", "total_count", "count", "命中", "match_total")


def _infer_total(courses: list[dict], program_facts) -> int | None:
    """从 program_facts 推断命中总数;取不到则返回 None(由调用方回退到 len(courses))。"""
    if isinstance(program_facts, dict):
        for k in _TOTAL_KEYS:
            v = program_facts.get(k)
            if isinstance(v, int) and v >= 0:
                return v
    return None


def build_facts(courses: list[dict], program_facts=None) -> str:
    """把检索结果序列化成喂给 LLM 的事实清单(确定性,不经过模型)。

    课程标题行写明命中总数,避免静默截断:总数优先取 program_facts 的「命中总数」,
    取不到就用 len(courses)。当总数 > 实际列出条数时,显式写「共 N 门,以下列出 M 门」。
    """
    lines: list[str] = []
    if courses:
        listed = len(courses)
        total = _infer_total(courses, program_facts)
        if total is None or total < listed:
            total = listed
        if total > listed:
            lines.append(f"课程(共 {total} 门,以下列出 {listed} 门):")
        else:
            lines.append(f"课程(共 {total} 门):")
        lines += [_fmt_course(c) for c in courses]
    if program_facts:
        # program_facts 结构任意,直接转 JSON 当补充事实,让 LLM 自行取用
        lines.append("补充事实(program_facts):")
        lines.append(json.dumps(program_facts, ensure_ascii=False, indent=None))
    return "\n".join(lines)


def guard_citations(text: str, courses: list[dict]) -> str:
    """生产护栏:剔除/标注回答里越界引用的课程码(不在输入 courses 课码集合内的)。

    逐行处理:某行若含输入之外的课程码,整行剔除(避免把虚构课信息留给用户)。
    剔除后在末尾追加一行警告,列出被剔除的越界课码,做到「skip 必有计数与原因」,不静默。
    program 名不算课码(_COURSE_CODE_RE 只匹配 4 字母+4 数字,不会误伤 program 名)。
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
        warn = f"[警告] 已剔除越界(疑似虚构)课程码:{'、'.join(sorted(dropped_codes))}"
        result = (result + "\n\n" + warn).strip() if result else warn
    return result


def answer(question: str, courses: list[dict], program_facts=None, topical: bool = False) -> str:
    """grounded 答案生成:无任何事实走固定句,否则把事实清单喂 qwen 生成中文回答。
    topical=True(semantic/hybrid 主题查询)追加相关性诚实指令:召回全是噪声则诚实兜底,不当 X 课列。"""
    if not courses and not program_facts:
        return EMPTY_ANSWER

    facts = build_facts(courses, program_facts)
    out = llm.call([
        {"role": "system", "content": _answer_system(topical)},
        {"role": "user", "content": USER_TMPL.format(q=question, facts=facts)},
    ]).strip()
    # 生产护栏:越界引用校验(原先只在 __main__,现移入生产路径)
    return guard_citations(out, courses)


def answer_stream(question: str, courses: list[dict], program_facts=None,
                  topical: bool = False) -> Iterator[str]:
    """流式 grounded 生成:逐 token yield 原始增量。无任何事实时 yield 固定句。
    护栏 guard_citations 需全文,由调用方(qa.run_stream)在收尾时对完整文本兜底。
    topical 含义同 answer():主题查询追加相关性诚实指令。"""
    if not courses and not program_facts:
        yield EMPTY_ANSWER
        return
    facts = build_facts(courses, program_facts)
    yield from llm.call_stream([
        {"role": "system", "content": _answer_system(topical)},
        {"role": "user", "content": USER_TMPL.format(q=question, facts=facts)},
    ])


# ---------- 知识库(FAQ / article)答案生成 ----------
# 弱召回拒答(不调 LLM):没检索到相关官方内容时,宁可说不确定 + 给官方入口(红线 3)。
KB_REFUSE = ("抱歉,我在已收录的 UQ 官方页面里没找到能直接回答这个问题的内容。"
             "建议到 my.UQ 学生支持页查询:https://my.uq.edu.au/ "
             "(课程、专业、选课相关的问题也可以直接问我)。")

KB_SYSTEM = """你是 UQ 学生事务助手。只能依据【资料】(来自 UQ 官方页面的片段)回答。
无论问题用中文还是英文提出,一律用简洁中文回答。
硬性规则:
- 【资料】已确认与问题相关,你必须依据它作答(英文资料转述成中文)。严禁回避、严禁输出
  「暂无信息」「无法回答」「资料未提及」之类的话;若资料只覆盖了问题的一部分,就回答能覆盖
  的部分,绝不因未完全覆盖而拒答。绝不编造步骤、数字、日期或网址。
- 涉及费用、截止日期、census date、退课/休学影响、考试安排等高风险信息,要提醒「以官方页面为准、注意时效」。
- 简短:不超过 6 句,或用要点列表;不寒暄、不重复问题。
- 不要自己写网址(系统会自动在末尾附上官方来源链接)。"""

KB_USER_TMPL = """问题:{q}

【资料】(UQ 官方页面片段)
{facts}

请只依据上面的【资料】用中文回答;若问的是具体日期/时间,从资料的日期清单里找出对应日期直接作答。"""


def _kb_facts(chunks: list[dict]) -> str:
    """把 KB chunk 序列化成编号事实清单(chunk.text 已含「页面标题 > h2 > h3」面包屑前缀)。"""
    return "\n\n".join(f"[{i}] {(c.get('text') or '').strip()}"
                       for i, c in enumerate(chunks, 1))


def kb_sources_block(chunks: list[dict]) -> str:
    """按 url 去重列出官方来源(红线 2:每个答案都带可一键核对的官方链接)。无来源返回空串。"""
    seen: set = set()
    items: list[str] = []
    for c in chunks:
        u = c.get("url")
        if not u or u in seen:
            continue
        seen.add(u)
        title = c.get("page_title") or c.get("breadcrumb") or u
        items.append(f"- {title}:{u}")
    return "\n\n来源(UQ 官方页面,可点击核对):\n" + "\n".join(items) if items else ""


# 高风险高频主题:用确定性答案,不交给 LLM 自由发挥(student-facing 红线 1)。
# 触发靠召回 top chunk 的页面标题(稳定标识),不靠脆弱的 query 关键词;不写死会变的具体日期。
_CENSUS_ANSWER = (
    "census date(普查日)是每个学习周期(study period)最终确定你选课状态的截止日期:"
    "只有在此日期前完成的加退课才算数。你在 census date 当天的选课情况,决定该学期的费用责任"
    "(应缴学费与各项费用),也是办理 HECS-HELP / FEE-HELP / SA-HELP、提供税号(TFN)等事项的"
    "截止日。具体日期因课程和教学周期而异:请登录 mySI-net 的「Enrolments」查看每门课的 census "
    "date,或在学校 important dates 页查询。请以官方页面为准、注意时效。"
)


def fixed_kb_body(chunks: list[dict]) -> str | None:
    """高风险主题(目前: census date)的确定性答案正文;命中返回模板,否则 None。

    触发靠召回 top chunk 的 page_title(稳定),内容忠实官方页面且不写死会变的具体日期(红线 1)。
    流式/非流式/收尾三路共用,保证 census 这类高频高风险问题答案 100% 一致、可核对。"""
    if not chunks:
        return None
    if "census date" in (chunks[0].get("page_title") or "").lower():
        return _CENSUS_ANSWER
    return None


# LLM 空答标记:chunks 已过 answerability 门(资料确认相关),正文却出现这些=异常空答,不能给学生
_KB_EMPTY_MARKERS = ("暂无信息", "无法回答", "无法回应", "资料未提及", "未提及相关",
                     "无相关", "没有相关信息", "暂无相关", "暂时无法", "无可用信息")

KB_RETRY_HINT = ("\n\n注意:以上【资料】已确认与该问题直接相关,请务必基于它用简洁中文给出"
                 "实质回答,严禁回答「暂无信息」之类的话。")


def is_empty_kb_answer(body: str) -> bool:
    """判断 KB 正文是否为异常空答(过短或命中空答标记);去掉「- 」「[1]」等前缀再看实质长度。
    流式收尾(qa.run_stream)也用它做兜底判断,故公开。"""
    stripped = body.strip().lstrip("-[]() .0123456789").strip()
    if len(stripped) < 8:
        return True
    return any(m in body for m in _KB_EMPTY_MARKERS)


def _gen_kb_body(question: str, chunks: list[dict], *, retry: bool = False) -> str:
    """调 LLM 生成 KB 正文;retry=True 时加强指令 + 升温,跳出 DeepSeek 对个别问题的空答倾向。"""
    user = KB_USER_TMPL.format(q=question, facts=_kb_facts(chunks))
    if retry:
        user += KB_RETRY_HINT
    return llm.call([
        {"role": "system", "content": KB_SYSTEM},
        {"role": "user", "content": user},
    ], temperature=(0.4 if retry else 0.0)).strip()


def _kb_fallback_body(chunks: list[dict]) -> str:
    """确定性降级:LLM 两次都空答时,给指向官方页面的确定性引导(绝不把空答留给学生,红线 3)。"""
    title = chunks[0].get("page_title") or chunks[0].get("breadcrumb") or "你的问题"
    return f"关于「{title}」,请查看下方 UQ 官方页面的说明(以官方页面为准,注意时效)。"


def kb_answer_body(question: str, chunks: list[dict]) -> str:
    """KB 正文(不含来源块):生成→空答重试(强指令+升温)→仍空确定性降级。

    chunks 非空=已过 answerability 门(资料确认相关),LLM 仍空答属异常(DeepSeek t=0 采样波动)。
    流式(qa.run_stream 收尾)与非流式共用此函数,保证两路兜底一致,学生永远看不到「暂无信息」。"""
    fixed = fixed_kb_body(chunks)
    if fixed:
        return fixed
    body = _gen_kb_body(question, chunks)
    if is_empty_kb_answer(body):
        body = _gen_kb_body(question, chunks, retry=True)
    if is_empty_kb_answer(body):
        body = _kb_fallback_body(chunks)
    return body


def answer_kb(question: str, chunks: list[dict]) -> str:
    """基于 KB chunk 的 grounded 答案:无 chunk 走拒答句;否则正文(含兜底)+ 代码确定性附官方来源。"""
    if not chunks:
        return KB_REFUSE
    return kb_answer_body(question, chunks) + kb_sources_block(chunks)


def answer_kb_stream(question: str, chunks: list[dict]) -> Iterator[str]:
    """流式 KB 答案:无 chunk yield 拒答句;否则逐 token 流式正文。
    来源块由调用方(qa.run_stream)在收尾追加,保证 100% 带官方链接。"""
    if not chunks:
        yield KB_REFUSE
        return
    yield from llm.call_stream([
        {"role": "system", "content": KB_SYSTEM},
        {"role": "user", "content": KB_USER_TMPL.format(q=question, facts=_kb_facts(chunks))},
    ])


# ---------- 单课详情答案(grounded 在官方大纲,兜底任意单课问题) ----------
# 清晰表述的高风险/精确问题(先修、考核占比、有没有某类考核…)已被前置确定性门拦截走结构化答案;
# 这里只兜底长尾问题(讲什么/适合谁/难不难/某字段是否提及…),全部 grounded 在喂入的完整记录。
COURSE_DETAIL_SYSTEM = """你是 UQ 选课助手。只依据【课程资料】(UQ 官方课程大纲)用简洁中文回答学生关于这门课的问题。
硬性规则:
- 只用【课程资料】里的信息,英文转述成中文;资料没覆盖的就直说「资料未提供」,绝不编造或猜测。
- 高风险/精确信息(先修要求、精确权重或分数、学分、日期):只能照搬资料原文,绝不改写其 and/or 逻辑或数字。
- 问「有没有某类考核」时据【考核项】如实回答有/无;但不要逐条罗列全部考核或复述精确权重(考核明细另行结构化展示)。
- 介绍类问题(讲什么/核心主题/适合谁修)按资料信息量自适应展开,不注水、不重复;不寒暄、不写网址、不重复课程码。"""

COURSE_DETAIL_USER = """课程:{code} {title}
学生的问题:{question}

【课程资料】
{facts}

请只依据上面的【课程资料】用中文回答学生的问题;资料未覆盖的部分明确说明,不要编造。"""


def _assessments_for_llm(course: dict) -> str:
    """考核项序列化给 LLM(含类别,供判断「有没有某类考核」);无数据返回空串。"""
    items = course.get("assessments")
    if not isinstance(items, list):
        return ""
    rows: list[str] = []
    for a in items:
        if not isinstance(a, dict):
            continue
        task = str(a.get("task") or "").strip() or "考核项"
        bits: list[str] = []
        if a.get("category"):
            bits.append(f"类别 {a['category']}")
        if a.get("weight") is not None:
            bits.append(f"{_fmt_num(a['weight'])}%")
        if a.get("hurdle"):
            bits.append("hurdle")
        rows.append(task + (f"[{','.join(bits)}]" if bits else ""))
    return ";".join(rows)


def _course_detail_facts(course: dict) -> str:
    """单课完整结构化记录序列化给 grounded LLM 兜底问答。
    高风险字段(先修原文/精确权重)随记录给出,prompt 要求逐字照搬不改写;
    清晰表述的高风险问题已被前置确定性门拦在 LLM 之前。"""
    parts: list[str] = []
    if course.get("description"):
        parts.append("课程简介:" + str(course["description"]))
    if course.get("topics"):
        parts.append("主题:" + str(course["topics"])[:600])
    if course.get("learning_outcomes"):
        parts.append("学习成果:" + str(course["learning_outcomes"])[:600])
    asmt = _assessments_for_llm(course)
    if asmt:
        parts.append("考核项:" + asmt)
    raw = (course.get("prerequisite_raw") or "").strip()
    parts.append("先修要求(原文):" + raw if raw else "先修要求:无")
    if course.get("units") is not None:
        parts.append(f"学分:{_fmt_num(course['units'])}")
    sems = [s for s in (course.get("semesters") or []) if s]
    if sems:
        parts.append("开课学期:" + "、".join(sems))
    locs = [l for l in (course.get("locations") or []) if l]
    if locs:
        parts.append("校区:" + "、".join(locs))
    return "\n\n".join(parts)


def _has_detail_content(course: dict) -> bool:
    """是否有可供 LLM 兜底作答的实质结构化内容;全空时走兜底文案不调 LLM。"""
    return bool(course.get("description") or course.get("topics")
                or course.get("learning_outcomes") or _has_assessments(course)
                or (course.get("prerequisite_raw") or "").strip())


# ---------- 单课结构化子问题(先修/考核/学分/开课):确定性作答,不交 LLM ----------
# 高成本事实(先修的 and/or 逻辑、考核权重数字、学分、开课)走结构化字段精准回应(红线1),
# 不让 LLM 自由转述以免改动逻辑/数字;意图判定走代码(规则12),无命中才回退 LLM 通用简介。
_DETAIL_INTENT_KW: dict[str, tuple[str, ...]] = {
    "prereq": ("先修", "先决", "前置", "前导", "修读要求", "prerequisite", "prereq"),
    "assessment": ("考核", "考评", "评估", "评分", "成绩构成", "怎么考", "如何考",
                   "考试", "占比", "assessment"),
    "units": ("学分", "几分", "多少分", "units"),
    "semester": ("开课", "哪个学期", "什么时候开", "第几学期", "何时开", "开设", "semester"),
}


def _detail_intents(question: str) -> list[str]:
    """命中的子问题意图键(确定性关键词),按 _DETAIL_INTENT_KW 固定顺序返回;无命中返回空表。"""
    q = (question or "").lower()
    return [key for key, kws in _DETAIL_INTENT_KW.items()
            if any(kw.lower() in q for kw in kws)]


def _fmt_num(x) -> str:
    """2.0 -> '2',22.5 -> '22.5'(去掉整数的 .0,保留真小数)。"""
    f = float(x)
    return str(int(f)) if f == int(f) else str(f)


def _detail_prereq(course: dict) -> str:
    code = course.get("code", "?")
    title = course.get("title") or ""
    raw = (course.get("prerequisite_raw") or "").strip().rstrip("。.")
    if not raw:
        return f"{code}({title})没有先修课要求。"
    return f"{code} 的先修课要求:{raw}。以官方课程页(ECP)为准。"


def _fmt_assessment_item(a: dict) -> str:
    """单个考核项 -> 'task(权重%、hurdle)';无权重/hurdle 时只出 task。"""
    task = str(a.get("task") or "").strip() or "考核项"
    extra: list[str] = []
    if a.get("weight") is not None:
        extra.append(f"{_fmt_num(a['weight'])}%")
    if a.get("hurdle"):
        extra.append("hurdle")
    return task + (f"({'、'.join(extra)})" if extra else "")


def _detail_assessment(course: dict) -> str:
    code = course.get("code", "?")
    items = course.get("assessments")
    parts = ([_fmt_assessment_item(a) for a in items if isinstance(a, dict)]
             if isinstance(items, list) else [])
    if not parts:
        return f"{code} 暂无结构化考核信息,请查看官方课程页(ECP)。"
    return f"{code} 的考核组成:" + "、".join(parts) + "。以官方课程页为准。"


def _detail_units(course: dict) -> str:
    code = course.get("code", "?")
    title = course.get("title") or ""
    u = course.get("units")
    if u is None:
        return f"{code} 暂无学分信息,请查看官方课程页。"
    return f"{code}({title})是 {_fmt_num(u)} 学分。"


def _detail_semester(course: dict) -> str:
    code = course.get("code", "?")
    sems = [s for s in (course.get("semesters") or []) if s]
    locs = [l for l in (course.get("locations") or []) if l]
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


# 低歧义考核类型查表(单课「有没有 X 考核」用):type -> (中文标签, 关键词)。
# 关键词同时用于「识别问的是哪类」(中英文)与「匹配考核 task/category」(数据多为英文)。
# 命名直白的类型才进表(加新类型 = 加一行);exam/midterm/group 等高歧义维度走专门三态分类器,不在此。
_ASSESSMENT_TYPES: dict[str, tuple[str, tuple[str, ...]]] = {
    "presentation": ("演讲/展示", ("presentation", "demonstration", "演讲", "展示", "汇报")),
    "quiz": ("测验", ("quiz", "测验", "小测")),
    "essay": ("论文", ("essay", "论文", "小论文")),
    "report": ("报告", ("report", "报告")),
    "project": ("项目", ("project", "项目")),
    "poster": ("海报", ("poster", "海报")),
    "portfolio": ("作品集", ("portfolio", "作品集")),
    "participation": ("课堂参与", ("participation", "参与", "出勤")),
    "reflection": ("反思", ("reflection", "reflective", "反思")),
}


def _matched_assessment_type(question: str) -> str | None:
    """问题命中的考核类型键(按查表顺序);无命中返回 None。"""
    q = (question or "").lower()
    for t, (_label, kws) in _ASSESSMENT_TYPES.items():
        if any(k.lower() in q for k in kws):
            return t
    return None


def _assessment_type_answer(question: str, course: dict) -> str | None:
    """单课「有没有 <某类> 考核」-> 据 assessments 确定性答有/无 + 命中项;问题未提类型返回 None。

    有考核数据才下「有/无」结论;无 assessments 数据归 unknown(refuse over wrong),绝不静默当「没有」。
    """
    t = _matched_assessment_type(question)
    if t is None:
        return None
    label, kws = _ASSESSMENT_TYPES[t]
    code = course.get("code", "?")
    items = course.get("assessments")
    if not isinstance(items, list) or not any(isinstance(a, dict) for a in items):
        return f"{code} 暂无结构化考核信息,无法确认是否有{label}考核,请查看官方课程页(ECP)。"
    kws_l = tuple(k.lower() for k in kws)
    matched = [
        a for a in items if isinstance(a, dict)
        and any(k in ((a.get("task") or "") + " " + (a.get("category") or "")).lower() for k in kws_l)
    ]
    if not matched:
        return f"{code} 没有{label}类考核。以官方课程页为准。"
    return f"{code} 有{label}类考核:" + "、".join(_fmt_assessment_item(a) for a in matched) + "。以官方课程页为准。"


def detail_structured_answer(question: str, course: dict) -> str | None:
    """命中先修/考核/学分/开课子问题 -> 用结构化字段确定性作答(每项一段);无命中返回 None。
    先判「有没有某类考核」(更具体),再判通用子问题。"""
    typed = _assessment_type_answer(question, course)
    if typed is not None:
        return typed
    intents = _detail_intents(question)
    if not intents:
        return None
    return "\n\n".join(_DETAIL_FMT[i](course) for i in intents)


def _has_assessments(course: dict) -> bool:
    """是否有可渲染的结构化考核项(任一 dict 带非空 task)。"""
    items = course.get("assessments")
    return isinstance(items, list) and any(
        isinstance(a, dict) and str(a.get("task") or "").strip() for a in items)


def _with_assessment_appendix(intro: str, course: dict) -> str:
    """通用简介后追加确定性考核组成(有数据才追加);考核仍走结构化字段,不交 LLM。"""
    if not _has_assessments(course):
        return intro
    return intro + "\n\n" + _detail_assessment(course)


def _detail_struct_context(course: dict, intents: list[str]) -> list[str]:
    """子问题确定性答案的事实依据(给 llm_judge faithfulness 用,与答案取自同一组字段)。"""
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


def answer_course_detail(question: str, course: dict | None) -> str:
    """单课问答:命中先修/考核/学分/开课子问题走结构化确定性答案(红线1),否则 LLM grounded 简介。
    结构化事实同时由前端详情卡展示;通用「讲什么」无子问题命中时交 LLM 生成中文简介。"""
    if not course:
        return "未找到该课程,请检查课程码是否正确。"
    structured = detail_structured_answer(question, course)
    if structured is not None:
        return structured
    if not _has_detail_content(course):
        intro = f"{course['code']} {course.get('title') or ''}。详细信息见下方课程卡与官方课程页。"
    else:
        intro = llm.call([
            {"role": "system", "content": COURSE_DETAIL_SYSTEM},
            {"role": "user", "content": COURSE_DETAIL_USER.format(
                code=course["code"], title=course.get("title") or "",
                question=question, facts=_course_detail_facts(course))},
        ]).strip()
    return _with_assessment_appendix(intro, course)


def answer_course_detail_stream(question: str, course: dict | None) -> Iterator[str]:
    """流式版单课问答:命中子问题 yield 结构化确定性答案(单块,不走 LLM);否则逐 token 流式简介。"""
    if not course:
        yield "未找到该课程,请检查课程码是否正确。"
        return
    structured = detail_structured_answer(question, course)
    if structured is not None:
        yield structured
        return
    if not _has_detail_content(course):
        yield f"{course['code']} {course.get('title') or ''}。详细信息见下方课程卡与官方课程页。"
    else:
        yield from llm.call_stream([
            {"role": "system", "content": COURSE_DETAIL_SYSTEM},
            {"role": "user", "content": COURSE_DETAIL_USER.format(
                code=course["code"], title=course.get("title") or "",
                question=question, facts=_course_detail_facts(course))},
        ])
    if _has_assessments(course):
        yield "\n\n" + _detail_assessment(course)


def gen_contexts(mode: str, courses: list[dict] | None = None, program_facts=None,
                 chunks: list[dict] | None = None, course: dict | None = None,
                 question: str | None = None) -> list[str]:
    """返回各 mode 实际喂给 LLM 的检索上下文,逐条(评测/调试用,与生产生成同源,零漂移)。

    复用生产序列化(_fmt_course / _kb_facts 同源 / _course_detail_facts),逐条对应 RAGAS
    的 retrieved_contexts;program/empty 等无 LLM 上下文的 mode 返回空列表。
    course_detail 命中子问题时返回结构化字段依据(答案确定性、非 LLM),与 program 同理。
    """
    if mode == "kb":
        return [(c.get("text") or "").strip() for c in (chunks or []) if c.get("text")]
    if mode == "course_detail":
        intents = _detail_intents(question or "")
        if intents:
            return _detail_struct_context(course or {}, intents)
        facts = _course_detail_facts(course or {})
        return [facts] if facts.strip() else []
    items = [_fmt_course(c) for c in (courses or [])]
    if program_facts:
        items.append("补充事实(program_facts):" + json.dumps(program_facts, ensure_ascii=False))
    return items


if __name__ == "__main__":
    # ---- 确定性自测(不依赖 Ollama,覆盖三个修复用例)----
    print("=== 确定性自测 ===")

    # 复现用例 A:无 title 的 program 课,不产空行且标出「必修」
    line = _fmt_course({"code": "COMP1200", "requirement_type": "core"})
    print("A 无title课:", line)
    assert line == "- COMP1200:必修", line
    assert line != "- COMP1200:", "不能产出空事实行"

    # 课程组 + 选修也要保留
    line2 = _fmt_course({"code": "COMP9999", "requirement_type": "elective",
                         "course_list": ["COMP1000", "COMP1100"]})
    print("A2 选修+课程组:", line2)
    assert "选修" in line2 and "课程组=COMP1000、COMP1100" in line2, line2

    # 真无任何信息也要保住课码 + 标注
    line3 = _fmt_course({"code": "MATHXXXX"})
    print("A3 纯课码:", line3)
    assert line3 == "- MATHXXXX:(本学期无开课信息)", line3

    # 复现用例 B:program_facts 给命中总数 71,只列 20 门 -> 事实写明总数
    listed = [{"code": f"COMP{1000+i}", "title": f"C{i}"} for i in range(20)]
    facts = build_facts(listed, program_facts={"命中总数": 71, "program": "BInfTech"})
    head = facts.splitlines()[0]
    print("B 标题行:", head)
    assert head == "课程(共 71 门,以下列出 20 门):", head

    # 总数等于列出条数时不写「列出 M 门」
    facts2 = build_facts(listed[:3])
    assert facts2.splitlines()[0] == "课程(共 3 门):", facts2.splitlines()[0]

    # 复现用例 C:护栏剔除越界(虚构)课程码,保留合法行,program 名不误伤
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

    # ---- 真实 Ollama 自测(无服务时跳过,不影响确定性结论)----
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
        # Ollama 未启动:显式报告跳过原因,不静默
        print(f"\n[跳过 Ollama 用例] 连不上服务:{e}")
