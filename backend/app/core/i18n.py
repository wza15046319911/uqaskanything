"""i18n.py — 答案语言识别 + 双语构造助手(中文/英文)。

用途:问题用什么语言提,答案就用什么语言答。语言识别是确定性分类(含 CJK 字符即中文,
否则英文),不走 LLM(全局规则 12);确定性答案的逐句构造(必修/选修、顿号 vs 逗号、
二选一 vs choose 1 of N、等 N 门)按 lang 分支,而不是把中文答案翻译过去——保持高风险事实
单一事实源、零误译(student-facing red line 1)。

固定句子放 MESSAGES 注册表,经 t(key, lang, **fmt) 取用;带分支逻辑的构造保留代码,只在
顿号/标签/量词等处调用本模块的小助手。
"""
from __future__ import annotations
import re
from typing import Literal

Lang = Literal["zh", "en"]

_CJK_RE = re.compile(r"[一-鿿㐀-䶿]")


def detect_lang(text: str) -> Lang:
    """问题文本含任意 CJK 字符 -> zh,否则 en(确定性,不走 LLM)。

    边界:一句英文里夹一个中文课名也判 zh。可接受,已记录;仅当评测确实误判时再上比例阈值。
    """
    return "zh" if _CJK_RE.search(text or "") else "en"


_CN_NUM = {2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九", 10: "十"}


def choice_word(k: int, lang: Lang) -> str:
    """k 门等价(可互换)课的措辞:zh -> 二选一/多选一;en -> 'choose 1 of N'。"""
    if lang == "en":
        return f"choose 1 of {k}"
    return f"{_CN_NUM.get(k, k)}选一"


def join_list(items, lang: Lang) -> str:
    """列表连接:zh 用顿号、,en 用逗号空格。"""
    return ("、" if lang == "zh" else ", ").join(items)


def n_courses(n: int, lang: Lang) -> str:
    """课程计数:zh -> 'N 门';en -> 'N course' / 'N courses'(按单复数)。"""
    if lang == "en":
        return f"{n} course" if n == 1 else f"{n} courses"
    return f"{n} 门"


def n_programs(n: int, lang: Lang) -> str:
    """专业计数:zh -> 'N 个';en -> 'N program' / 'N programs'。"""
    if lang == "en":
        return f"{n} program" if n == 1 else f"{n} programs"
    return f"{n} 个"


def label_req(req: str | None, lang: Lang) -> str:
    """需求类型标签:core/compulsory -> 必修/compulsory,elective -> 选修/elective,None -> 空串。"""
    if req in ("core", "compulsory", "required"):
        return "必修" if lang == "zh" else "compulsory"
    if req in ("elective", "option", "optional"):
        return "选修" if lang == "zh" else "elective"
    return ""


# 固定句子注册表(无分支逻辑的整句);带逻辑的构造不放这里,见各 _ans_* 构造函数。
# REVIEW: 英文(en)高风险句须人工逐句复核后再上线(student-facing red line 6):
#   census / kb_refuse / kb_fallback_body 直接影响学生选课/费用决定。
MESSAGES: dict[str, dict[Lang, str]] = {
    # 课程检索无结果(answer.py answer/answer_stream)
    "empty_answer": {
        "zh": "没有找到符合条件的课程。",
        "en": "No matching courses found.",
    },
    # 未找到课程(answer.py course_detail)
    "course_not_found": {
        "zh": "未找到该课程,请检查课程码是否正确。",
        "en": "Course not found; please check the course code.",
    },
    # 无结构化内容时的单课兜底(answer.py course_detail)
    "detail_see_card": {
        "zh": "{code} {title}。详细信息见下方课程卡与官方课程页。",
        "en": "{code} {title}. See the course card below and the official course page for details.",
    },
    # 未找到专业(qa.py program 路由)
    "program_not_found": {
        "zh": "未找到名为「{name}」的专业,试试全称(如 Bachelor of Computer Science)。",
        "en": "No program named '{name}' was found. Try the full name (e.g. Bachelor of Computer Science).",
    },
    # 问题太宽泛(qa.py EMPTY_MSG)
    "empty_msg": {
        "zh": "问题太宽泛或无法形成检索条件,请补充学科方向或筛选条件(如学期 / 有无考试 / 专业)。",
        "en": "The question is too broad to form a search. Please add a subject area or filters "
              "(e.g. semester / with or without exam / program).",
    },
    # KB 弱召回拒答(red line 3:不确定就给官方入口,不硬猜)
    "kb_refuse": {
        "zh": ("抱歉,我在已收录的 UQ 官方页面里没找到能直接回答这个问题的内容。"
               "建议到 my.UQ 学生支持页查询:https://my.uq.edu.au/ "
               "(课程、专业、选课相关的问题也可以直接问我)。"),
        "en": ("Sorry, I could not find content in the indexed official UQ pages that directly "
               "answers this question. Please check the my.UQ student support page: "
               "https://my.uq.edu.au/ (you can also ask me about courses, programs, and enrolment)."),
    },
    # census date 确定性模板(high-risk,red line 1:不让 LLM 自由发挥;不写死会变的具体日期)
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
    # KB 来源块标题(red line 2:每条答案带可点核对的官方链接)
    "kb_sources_header": {
        "zh": "\n\n来源(UQ 官方页面,可点击核对):\n",
        "en": "\n\nSources (official UQ pages, click to verify):\n",
    },
    # KB 两次都空答时的确定性兜底(red line 3:绝不给学生留空答)
    "kb_fallback_body": {
        "zh": "关于「{title}」,请查看下方 UQ 官方页面的说明(以官方页面为准,注意时效)。",
        "en": "For '{title}', please see the official UQ page below (rely on the official page; mind the timeliness).",
    },
}


def t(key: str, lang: Lang, **fmt) -> str:
    """取固定句子并按 lang 选择;**fmt 做 str.format 占位填充。缺 key 直接抛错,不静默兜底(规则 19)。"""
    entry = MESSAGES[key]
    s = entry.get(lang) or entry["zh"]
    return s.format(**fmt) if fmt else s
