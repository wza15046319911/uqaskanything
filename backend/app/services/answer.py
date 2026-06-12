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


def answer(question: str, courses: list[dict], program_facts=None) -> str:
    """grounded 答案生成:无任何事实走固定句,否则把事实清单喂 qwen 生成中文回答。"""
    if not courses and not program_facts:
        return EMPTY_ANSWER

    facts = build_facts(courses, program_facts)
    out = llm.call([
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": USER_TMPL.format(q=question, facts=facts)},
    ]).strip()
    # 生产护栏:越界引用校验(原先只在 __main__,现移入生产路径)
    return guard_citations(out, courses)


def answer_stream(question: str, courses: list[dict], program_facts=None) -> Iterator[str]:
    """流式 grounded 生成:逐 token yield 原始增量。无任何事实时 yield 固定句。
    护栏 guard_citations 需全文,由调用方(qa.run_stream)在收尾时对完整文本兜底。"""
    if not courses and not program_facts:
        yield EMPTY_ANSWER
        return
    facts = build_facts(courses, program_facts)
    yield from llm.call_stream([
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": USER_TMPL.format(q=question, facts=facts)},
    ])


# ---------- 知识库(FAQ / article)答案生成 ----------
# 弱召回拒答(不调 LLM):没检索到相关官方内容时,宁可说不确定 + 给官方入口(红线 3)。
KB_REFUSE = ("抱歉,我在已收录的 UQ 官方页面里没找到能直接回答这个问题的内容。"
             "建议到 my.UQ 学生支持页查询:https://my.uq.edu.au/ "
             "(课程、专业、选课相关的问题也可以直接问我)。")

KB_SYSTEM = """你是 UQ 学生事务助手。只能依据【资料】(来自 UQ 官方页面的片段)用简洁中文回答。
硬性规则:
- 资料里有相关内容就据此用简洁中文作答(英文资料要转述成中文),不要回避、不要说「暂无信息」;
  只有资料确实没提到时才说不确定。绝不编造步骤、数字、日期或网址。
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


def answer_kb(question: str, chunks: list[dict]) -> str:
    """基于 KB chunk 的 grounded 答案:无 chunk 走拒答句;否则 LLM 生成正文 + 代码确定性附官方来源。"""
    if not chunks:
        return KB_REFUSE
    body = llm.call([
        {"role": "system", "content": KB_SYSTEM},
        {"role": "user", "content": KB_USER_TMPL.format(q=question, facts=_kb_facts(chunks))},
    ]).strip()
    return body + kb_sources_block(chunks)


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
