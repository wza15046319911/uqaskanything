"""
planner.py — 阶段五:自然语言 -> 查询计划(Query Plan)
在 query.py 的基础上做强:多一个 program 模式(课程<->专业),并把后端做成可插拔
(默认本地 Ollama qwen2.5-coder,可切 DeepSeek)。

分工(确定性决策用代码,语言任务交模型):
  - LLM 只做语言活:判 mode、写 WHERE、给英文 semantic_query、抽 course_code/program_name/direction
  - 代码做确定性活:schema 实时注入、JSON 解析、WHERE 合法性拦截、缺失字段纠偏兜底

公开接口:
  - build_schema_doc(conn) -> str
  - plan(question, schema_doc=None, conn=None) -> dict
    返回 {mode, where, semantic_query, course_code, program_name, direction}
    mode ∈ {filter, semantic, hybrid, program, kb, course_detail}

后端开关:见 llm 模块。设了 DEEPSEEK_API_KEY(可写进 .env)就全程走 DeepSeek,否则本地 Ollama。

用法:
    python planner.py "CS有哪些课程没有考试"
"""
from __future__ import annotations
import os
import re
import json
import argparse

import psycopg

from app.services import llm

from app.core.config import DSN

LOWCARD = ["semester", "location", "attendance_mode", "level"]  # 低基数列,枚举值实时取

# build_schema_doc 取到的真实枚举集合,模块级缓存供确定性兜底校验用(列名 -> 小写值集合)。
_ENUM_CACHE: dict[str, set[str]] = {}

# 学位串显式信号:出现这些才允许 program_to_courses(用户明确给了学位名)。
PROGRAM_NAME_RE = re.compile(
    r"(bachelors?\s+of|masters?\s+of|diploma|graduate\s+certificate|graduate\s+diploma|doctors?\s+of|"
    r"学士|硕士|博士|本科专业|研究生专业|文凭)", re.I)

# 学分提取:'2学分' / '2 units' 等,用于撤销误判 program 后确定性找回结构化条件。
UNITS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:学分|units?)", re.I)

# 课型/要求关键词:出现这些说明问的是某专业"要修哪些(核心/必修/选修)课"。
REQ_KW_RE = re.compile(
    r"核心课|核心|必修|选修|compulsory|(?<![A-Za-z])core(?![A-Za-z])|"
    r"(?<![A-Za-z])elective(?![A-Za-z])|要修哪些|有哪些课|哪些课|培养方案|课表", re.I)
# 课-专业关系关键词:课程码 + 这些 = "这门课是哪些专业的必修/选修"。
PROG_REL_KW_RE = re.compile(r"专业|program|major|主修|培养方案|必修|选修|核心", re.I)
# 许可关键词:课码 + 学位名 + 这些 = "某专业能不能修某课"(禁课查询)。
PERMIT_KW_RE = re.compile(r"能修|能不能|可以修|可不可以|不能修|禁修|不可修|can\s+(?:i\s+)?take|allowed to take", re.I)
# 学位全名抽取(英文):喂给 find_program 的 ILIKE,允许字母/空格/斜杠/&/括号/连字符。
_PROG_NAME_EXTRACT = re.compile(
    r"((?:bachelors?|masters?|graduate\s+diploma|graduate\s+certificate|diploma|doctor)"
    r"\s+(?:of\s+)?[A-Za-z][A-Za-z /&()'\-]*)", re.I)
# 学历层级关键词 -> 确定性 level 字面值(规则 12:同样输入永远同样映射)。
# 只用"研究生/本科/postgraduate/undergraduate",不用"master/bachelor"(那是 program 信号)。
_LEVEL_KW = [
    (re.compile(r"研究生|postgraduate|post-graduate", re.I), "Postgraduate Coursework"),
    (re.compile(r"本科生?|undergraduate|under-graduate", re.I), "Undergraduate"),
]

MODES = ("filter", "semantic", "hybrid", "program", "kb", "course_detail")

# WHERE 只允许这些结构化枚举/数值列;文本列(title/code/description...)严禁出现
ALLOWED_WHERE_COLS = {"semester", "year", "location", "attendance_mode",
                      "level", "units", "has_exam", "has_hurdle"}
TEXT_COLS = re.compile(r"\b(title|code|description|search_blob|learning_outcomes|topics|coordinator|coordinating_unit)\b", re.I)
LIKE_RE = re.compile(r"\b(like|ilike|similar\s+to)\b", re.I)
BANNED = re.compile(r"(;|--|/\*|\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|select)\b)", re.I)
# 课程码:4 字母+4 数字。中文紧贴时 ASCII \b 不成立,改用环视边界;
# 末尾禁字母/数字,避免把 CSSE10012(5 位数字)误判成 CSSE1001。
COURSE_CODE_RE = re.compile(r"(?<![A-Za-z])([A-Za-z]{4}\d{4})(?![A-Za-z0-9])")

# 缩写 -> 英文学科,补 semantic_query 用。
# 边界用 (?<![A-Za-z])X(?![A-Za-z]):中文紧贴缩写时(如 "CS有") ASCII \b 不成立,故改用环视。
ABBR = {"cs": "computer science", "ai": "artificial intelligence", "ml": "machine learning",
        "it": "information technology", "ee": "electrical engineering"}
_ABBR_RE = {a: re.compile(rf"(?<![A-Za-z]){a}(?![A-Za-z])", re.I) for a in ABBR}

# 主题词探测:出现这些(学科/缩写/“相关/about”)说明问题里含模糊主题,必须有 semantic_query
TOPIC_HINT = re.compile(
    r"(相关|有关|关于|方向|领域|主题|about|related|topic|"
    r"计算机|软件|人工智能|机器学习|深度学习|数据|网络安全|信息安全|金融|会计|经济|"
    r"心理|生物|化学|物理|数学|统计|电子|电气|通信|机械|土木|商科|管理|市场|营销|"
    r"写作|护理|艺术|法律|医学|教育|建筑|环境|机器人|"
    r"(?<![A-Za-z])(cs|ai|ml|it|ee)(?![A-Za-z])|"
    r"computer|software|machine\s*learning|"
    r"data|security|finance|account|psycholog|biolog|chemis|physic|statistic|"
    r"electric|mechanic|civil|business|market|"
    r"writing|nursing|\bart\b|\blaw\b|medic|education|architect|environment|robotic|engineer)", re.I)


PROMPT = """你是 UQ 课程库查询规划器。把用户问题转成 JSON 查询计划,只输出 JSON,不要解释。
{schema}

mode 一共 5 种:
- "filter":只有结构化条件(学期/有无考试/hurdle/本研/学分/校区等),没有模糊主题。给 where,semantic_query 留空。
- "semantic":只有模糊主题/学科(如"跟机器学习相关"),没有结构化条件。给英文 semantic_query,where 留空。
- "hybrid":既有结构化条件又有模糊主题/学科。where 和 semantic_query 都给。
- "program":问"课程 <-> 专业"的关系。识别两种方向:
    · direction="course_to_programs":问"某门课(给了课程码,如 CSSE1001)是哪些专业的必修/选修"。填 course_code。
    · direction="program_to_courses":问"某个专业(如 Bachelor of Computer Science)要修哪些课"。填 program_name。
- "kb":问的是学校事务/政策/日期/服务,而不是具体课程或专业。例如:开学/census/缴费/退课截止等日期、
  重置密码、申请缓考、假期开放时间、停车收费、遭遇骚扰或霸凌求助、开具在读证明等。所有字段留空。
  **只要问题里出现课程码(如 CSSE1001)或学位名(Bachelor of…/学士/硕士),就不是 kb。**

【关键规则】
- 学科/专业方向/主题(计算机/人工智能/金融/网络安全/心理学…)一律走 semantic_query,**用英文**表达;
  **绝不能**用 title/code/description 做 LIKE 匹配(课名是英文、学科横跨多个课程码)。
- 缩写也算学科,必须翻成英文放进 semantic_query,**绝不能因为不认识就丢弃**:
  CS=computer science、AI=artificial intelligence、ML=machine learning、IT=information technology、EE=electrical engineering。
- where 只能用这些列:semester, year, location, attendance_mode, level, units, has_exam, has_hurdle。
  字符串用单引号,布尔写 true/false(不加引号),不写分号/SELECT/LIKE,绝不碰 title/code/description 等文本列。
- **绝不替换用户没说的值**:只能把用户原话里出现的校区/学期/层级照搬进 where。
  若用户要的 location/semester/level 不在 schema 所列枚举内(例如用户问 Gatton 但枚举只有 St Lucia),
  就**原样用用户的字面值**(如 location='Gatton'),让结果正确为空;**绝不能**擅自换成枚举里已知的值(如 St Lucia)。
- 课程码形如 4 个字母+4 个数字(CSSE1001)。问题里出现课程码且在问"哪些专业",就是 program/course_to_programs。
- 严格输出这个结构(用不到的字段给空字符串):
  {{"mode":"...","where":"...","semantic_query":"...","course_code":"...","program_name":"...","direction":"..."}}

例子:
- "没有考试的课" -> {{"mode":"filter","where":"has_exam=false","semantic_query":"","course_code":"","program_name":"","direction":""}}
- "没有考试的研究生课" -> {{"mode":"filter","where":"level='Postgraduate Coursework' AND has_exam=false","semantic_query":"","course_code":"","program_name":"","direction":""}}
- "找跟机器学习相关的课" -> {{"mode":"semantic","where":"","semantic_query":"machine learning","course_code":"","program_name":"","direction":""}}
- "跟机器学习相关的课" -> {{"mode":"semantic","where":"","semantic_query":"machine learning","course_code":"","program_name":"","direction":""}}
- "CS有哪些课程没有考试" -> {{"mode":"hybrid","where":"has_exam=false","semantic_query":"computer science","course_code":"","program_name":"","direction":""}}
- "计算机相关、没有hurdle的研究生课" -> {{"mode":"hybrid","where":"level='Postgraduate Coursework' AND has_hurdle=false","semantic_query":"computer science","course_code":"","program_name":"","direction":""}}
- "CSSE1001是哪些专业的必修" -> {{"mode":"program","where":"","semantic_query":"","course_code":"CSSE1001","program_name":"","direction":"course_to_programs"}}
- "Bachelor of Computer Science 要修哪些课" -> {{"mode":"program","where":"","semantic_query":"","course_code":"","program_name":"Bachelor of Computer Science","direction":"program_to_courses"}}
- "census date 是什么时候" -> {{"mode":"kb","where":"","semantic_query":"","course_code":"","program_name":"","direction":""}}
- "怎么重置 UQ 密码" -> {{"mode":"kb","where":"","semantic_query":"","course_code":"","program_name":"","direction":""}}
- "圣诞假期图书馆开放吗" -> {{"mode":"kb","where":"","semantic_query":"","course_code":"","program_name":"","direction":""}}
- "St Lucia 校区停车怎么收费" -> {{"mode":"kb","where":"","semantic_query":"","course_code":"","program_name":"","direction":""}}

用户问题:{q}"""


def build_schema_doc(conn) -> str:
    """实时取低基数枚举列的 distinct 值注入,保证 LLM 用真实枚举写 WHERE。"""
    enums = {c: [r[0] for r in conn.execute(
        f"SELECT DISTINCT {c} FROM courses WHERE {c} IS NOT NULL ORDER BY 1")]
        for c in LOWCARD}
    # 真实枚举集合写入模块缓存,供 plan() 确定性校验「LLM 没擅自换值」。
    _ENUM_CACHE.update({c: {str(v).strip().lower() for v in vals} for c, vals in enums.items()})
    return f"""表 courses(每行一门课):
  code TEXT              课程码,如 CSSE1001(文本列,严禁进 where)
  title TEXT             课程名(文本列,严禁进 where)
  semester TEXT          实际值:{enums['semester']}
  year INT               年份,如 2026
  location TEXT          实际值:{enums['location']}
  attendance_mode TEXT   实际值:{enums['attendance_mode']}
  level TEXT             实际值:{enums['level']}
  units REAL             学分
  has_exam BOOLEAN       是否含考试(写 true/false,不加引号)
  has_hurdle BOOLEAN     是否含 hurdle 评估(true/false)
  (description / learning_outcomes / topics 等文本不在结构化列里,模糊主题要走 semantic_query)
表 programs(专业):program_id, title, total_units, rules
表 program_course(专业-课程扁平):program_id, course_code, requirement_type('core'|'elective')
  -> 课程<->专业关系问题走 mode='program'。"""


# ---------- LLM 后端(可插拔)----------

def _call_llm(prompt: str) -> str:
    """单条 user prompt -> LLM 返回的 JSON 字符串。后端(本地 Ollama / DeepSeek)由 llm 模块按 env 选。"""
    return llm.call([{"role": "user", "content": prompt}], json_mode=True)


# ---------- 确定性校验 / 兜底 ----------

def _clean_where(where: str) -> str:
    """WHERE 合法性确定性拦截:非法(SELECT/分号/文本列/LIKE)一律清空,绝不放行。"""
    if not where or not where.strip():
        return ""
    w = where.strip()
    # 先剥离单引号字符串字面量(同 retrieval.guard_where),避免值里含 select/and
    # 或像 location='Select Campus' 这种被 BANNED/白名单误杀整段。
    stripped = re.sub(r"'[^']*'", "''", w)
    if BANNED.search(stripped) or LIKE_RE.search(stripped) or TEXT_COLS.search(stripped):
        return ""
    # 列白名单:在「剥离字面量后」的串上找「标识符 紧跟比较运算符」,不在白名单 -> 判非法清空
    for ident in re.findall(r"\b([a-zA-Z_]+)\s*(?:=|<|>|!=|<=|>=|\bin\b|\bis\b)", stripped, re.I):
        if ident.lower() in {"and", "or", "not", "true", "false", "null", "is", "in"}:
            continue
        if ident.lower() not in ALLOWED_WHERE_COLS:
            return ""
    return w


def _has_topic(question: str) -> bool:
    return bool(TOPIC_HINT.search(question))


# UQ 已知校区白名单(确定性查表),原文写法 -> 规范 location 字面值。
# 用户问到非 St Lucia 校区时,库里没有,必须返回空,绝不被 LLM 换成 St Lucia。
_CAMPUS_LITERALS = {
    "st lucia": "St Lucia", "stlucia": "St Lucia", "圣卢西亚": "St Lucia",
    "gatton": "Gatton", "加顿": "Gatton",
    "herston": "Herston", "赫斯顿": "Herston",
    "dutton park": "Dutton Park", "duttonpark": "Dutton Park",
    "pace": "PACE", "translational research institute": "Translational Research Institute",
    "external": "External", "online": "Online",
}
_CAMPUS_RE = {key: re.compile(rf"(?<![A-Za-z]){re.escape(key)}(?![A-Za-z])", re.I)
              for key in _CAMPUS_LITERALS if key.isascii()}


def _enforce_enum_guard(where: str, question: str) -> str:
    """确定性兜底:问题提到某校区时,保证 where 的 location 条件不被 LLM 漏写或篡改。

    - 校区不在真实枚举内(Gatton/Herston…):强制把 location 改成「用户原校区字面值」
      (使 SQL 命中 0),绝不留成 St Lucia。核心不变量:问非 St Lucia 校区必须返回空。
    - 校区在枚举内(St Lucia):LLM 已写 location 就尊重;漏写则确定性补回——否则
      「St Lucia 校区的人工智能课」会丢掉校区过滤,退化成全库语义检索。
    """
    if not where:
        where = ""
    loc_enum = _ENUM_CACHE.get("location", set())
    # 检测问题里提到的校区(查表确定性匹配),取第一个命中的规范字面值
    asked = None
    for key, literal in _CAMPUS_LITERALS.items():
        rx = _CAMPUS_RE.get(key)
        if rx is not None:
            if rx.search(question):
                asked = literal
                break
        elif key in question:  # 非 ASCII(中文)直接子串匹配
            asked = literal
            break
    if asked is None:
        return where
    has_loc = bool(re.search(r"\blocation\s*=", where, re.I))
    forced = f"location='{asked}'"
    if asked.lower() in loc_enum:
        # 在枚举内:LLM 写了 location 就放行,漏写则补回(不覆盖,避免改掉 LLM 写对的值)
        if has_loc:
            return where
        return f"{where.strip()} AND {forced}" if where.strip() else forced
    # 不在枚举内:把 where 里已有的 location 等值条件替成用户原校区;没有就追加一个。
    if has_loc:
        return re.sub(r"\blocation\s*=\s*'[^']*'", forced, where, flags=re.I)
    return f"{where.strip()} AND {forced}" if where.strip() else forced


def _fallback_semantic(question: str) -> str:
    """问题明显含主题但 LLM 没给 semantic_query 时,确定性补一个英文学科词。"""
    for abbr, full in ABBR.items():
        if _ABBR_RE[abbr].search(question):
            return full
    # 没识别到缩写就用整句兜底(bge-m3 多语,中文也能检),交给向量层
    return question.strip()


def _extract_program_name(question: str) -> str:
    """从问题里抠出英文学位全名(喂 find_program 的 ILIKE 子串)。"""
    m = _PROG_NAME_EXTRACT.search(question)
    return m.group(1).strip() if m else ""


def _force_program_route(question: str) -> tuple[str, str, str] | None:
    """确定性判断是否为 program 查询(规则 12,兜 LLM 路由抖动)。
    返回 (direction, course_code, program_name) 或 None(不强制)。
      · 课码 + 学位全名 + 能否修关键词 -> permit(禁课/许可查询)
      · 课程码 + 专业/必修/选修关键词  -> course_to_programs
      · 学位全名 + 课型关键词         -> program_to_courses
    """
    code = COURSE_CODE_RE.search(question)
    has_degree = PROGRAM_NAME_RE.search(question)
    if code and has_degree and PERMIT_KW_RE.search(question):
        return ("permit", code.group(1).upper(), _extract_program_name(question))
    if code and PROG_REL_KW_RE.search(question):
        return ("course_to_programs", code.group(1).upper(), "")
    if has_degree and REQ_KW_RE.search(question):
        return ("program_to_courses", "", _extract_program_name(question))
    return None


def _enforce_level_hint(where: str, question: str) -> str:
    """确定性注入 level 过滤:问题提"研究生/本科"但 where 未含 level 时补上(规则 12)。"""
    if re.search(r"\blevel\s*=", where or "", re.I):
        return where                       # LLM 已给 level,尊重之
    for rx, val in _LEVEL_KW:
        if rx.search(question):
            return f"{where.strip()} AND level='{val}'" if (where or "").strip() else f"level='{val}'"
    return where


def plan(question: str, schema_doc: str | None = None, conn: object | None = None) -> dict:
    """自然语言 -> 查询计划 dict。

    schema_doc 缺省时若给了 conn 就实时构建;两者都没有则用一份静态 schema(枚举占位)。
    返回固定 6 字段:{mode, where, semantic_query, course_code, program_name, direction}。
    """
    if schema_doc is None:
        if conn is not None:
            schema_doc = build_schema_doc(conn)
        else:
            schema_doc = ("表 courses 列:semester, year, location, attendance_mode, "
                          "level('Undergraduate'|'Postgraduate Coursework'), units, "
                          "has_exam(bool), has_hurdle(bool);文本列 code/title/description 不进 where。")

    raw = _call_llm(PROMPT.format(schema=schema_doc, q=question))
    try:
        p = json.loads(raw)
    except json.JSONDecodeError:
        # 解析失败先抠出首个 {...} 再试(模型有时在 JSON 外带解释/markdown 围栏)
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            raise ValueError(f"LLM 返回非法 JSON:{raw!r}")
        try:
            p = json.loads(m.group(0))
        except json.JSONDecodeError as e:
            raise ValueError(f"LLM 返回非法 JSON:{raw!r}") from e

    # where 必须是字符串;非 str(对象/列表/数字)直接判非法清空,不 stringify。
    raw_where = p.get("where", "")
    where_str = raw_where.strip() if isinstance(raw_where, str) else ""

    # 归一化所有字段为字符串
    out = {
        "mode": str(p.get("mode", "")).strip().lower(),
        "where": where_str,
        "semantic_query": str(p.get("semantic_query", "") or "").strip(),
        "course_code": str(p.get("course_code", "") or "").strip().upper(),
        "program_name": str(p.get("program_name", "") or "").strip(),
        "direction": str(p.get("direction", "") or "").strip().lower(),
    }

    if out["mode"] not in MODES:
        raise ValueError(f"非法 mode={out['mode']!r}(原始 {p!r})")

    # WHERE 确定性清洗:含文本列/LIKE/SELECT 一律清空
    out["where"] = _clean_where(out["where"])
    # 确定性枚举兜底:用户问非枚举校区时,强制 where 用用户原校区字面值(使结果正确为空),
    # 绝不放任 LLM 把 Gatton 换成 St Lucia 返回全库。
    out["where"] = _enforce_enum_guard(out["where"], question)

    # 确定性 program 强制(规则 12):LLM 偶发把明确的"专业↔课程"查询误路由到 semantic/filter,这里纠偏。
    forced = _force_program_route(question)
    if forced:
        out["mode"] = "program"
        out["direction"], _fc, _fn = forced
        if _fc:
            out["course_code"] = _fc
        if out["direction"] in ("program_to_courses", "permit"):
            out["program_name"] = out["program_name"] or _fn
    elif (COURSE_CODE_RE.search(question)
          and not PROGRAM_NAME_RE.search(question)
          and not PROG_REL_KW_RE.search(question)):
        # 课程码 + 无学位名 + 无「专业/必修/选修」关系词 -> 单门课详情(介绍/先修/考核/学分)
        out["mode"] = "course_detail"
        out["course_code"] = COURSE_CODE_RE.search(question).group(1).upper()
        out["where"] = ""
        out["semantic_query"] = ""
        out["program_name"] = ""
        out["direction"] = ""
        return out

    # kb 前置分类:纯学校事务/政策/日期/服务问题,直接转知识库,不进课程库逻辑。
    # 确定性保险(规则 12):含课程码或学位全名时一定是课程/专业问题,撤销 kb 交回课程路由。
    if out["mode"] == "kb":
        if COURSE_CODE_RE.search(question) or PROGRAM_NAME_RE.search(question):
            out["mode"] = "semantic"
        else:
            out["where"] = ""
            out["semantic_query"] = ""
            out["course_code"] = ""
            out["program_name"] = ""
            out["direction"] = ""
            return out

    if out["mode"] == "program":
        # program 模式:补全 course_code / direction
        if not out["course_code"]:
            m = COURSE_CODE_RE.search(question)
            if m:
                out["course_code"] = m.group(1).upper()
        if out["direction"] not in ("course_to_programs", "program_to_courses", "permit"):
            # 有课程码默认问“这门课在哪些专业”,否则当“专业要修哪些课”
            out["direction"] = "course_to_programs" if out["course_code"] else "program_to_courses"
        # permit(能否修某课):课码 + 学位名都需要,直接返回
        if out["direction"] == "permit":
            if not out["program_name"]:
                out["program_name"] = _extract_program_name(question)
            out["where"] = ""
            out["semantic_query"] = ""
            return out
        # 触发收紧:program_to_courses 仅当问题里出现明确学位串(Bachelor of/Master of/学士/硕士…)
        # 才放行;否则是 LLM 凭空脑补了专业名(用户没说),撤销 program 改走 topic。
        if out["direction"] == "program_to_courses" and not PROGRAM_NAME_RE.search(question):
            out["mode"] = ""  # 标记撤销,落到下面的 topic/semantic 兜底
            out["course_code"] = ""
            out["program_name"] = ""
            out["direction"] = ""
            # LLM 误判 program 时常丢掉结构化条件;确定性找回 units(学分)以便走 hybrid。
            if not out["where"]:
                mu = UNITS_RE.search(question)
                if mu:
                    out["where"] = f"units={mu.group(1)}"
        else:
            # program 模式不带结构化/语义条件,清掉避免误用
            out["where"] = ""
            out["semantic_query"] = ""
            return out

    # 非 program:清空专业相关字段
    out["course_code"] = ""
    out["program_name"] = ""
    out["direction"] = ""

    topic = _has_topic(question)
    # 确定性 level 兜底:问"研究生/本科"但 where 未含 level 时补上(规则 12),修 LLM 漏过滤。
    out["where"] = _enforce_level_hint(out["where"], question)

    # program 被撤销(mode="")后需要落到一个有效 mode:有 where 走 filter,否则 semantic。
    if out["mode"] == "":
        out["mode"] = "filter" if out["where"] else "semantic"
    # level-hint 给 semantic 补了 where:有主题升 hybrid,无主题降 filter。
    if out["where"] and out["mode"] == "semantic":
        out["mode"] = "hybrid" if topic else "filter"

    # 兜底 1:问题含主题但 mode 落到 filter -> 升级 hybrid(有 where)或 semantic(无 where)
    if topic and out["mode"] == "filter":
        out["mode"] = "hybrid" if out["where"] else "semantic"

    # 兜底 2:semantic/hybrid 缺 semantic_query 且问题含主题 -> 确定性补英文学科词
    if out["mode"] in ("semantic", "hybrid") and not out["semantic_query"]:
        if topic:
            out["semantic_query"] = _fallback_semantic(question)
        elif out["mode"] == "hybrid":
            # hybrid 却没主题词且补不出来 -> 退回 filter
            out["mode"] = "filter"
        else:
            raise ValueError(f"semantic 模式缺 semantic_query 且问题无主题词:{question!r}")

    # 兜底 3:filter/hybrid 必须有合法 where
    if out["mode"] in ("filter", "hybrid") and not out["where"]:
        if out["semantic_query"]:
            out["mode"] = "semantic"   # 只剩语义,降级
        else:
            raise ValueError(f"{out['mode']} 模式无合法 where 也无 semantic_query:{question!r}")

    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question", help="自然语言问题")
    args = ap.parse_args()
    with psycopg.connect(DSN) as conn:
        conn.read_only = True
        schema = build_schema_doc(conn)
    print(json.dumps(plan(args.question, schema_doc=schema), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    # 真实 DB + Ollama 自测
    import sys
    if len(sys.argv) > 1:
        main()
    else:
        with psycopg.connect(DSN) as conn:
            conn.read_only = True
            schema = build_schema_doc(conn)
        cases = [
            "CS有哪些课程没有考试",
            "没有考试的研究生课",
            "跟机器学习相关的课",
            "CSSE1001是哪些专业的必修",
            "Bachelor of Computer Science 要修哪些课",
            "St Lucia 校区 2 学分的本科课",
        ]
        print(f"[backend={llm.backend_name()}] schema 注入枚举:level/semester/location/attendance_mode 已实时取\n")
        for q in cases:
            try:
                pl = plan(q, schema_doc=schema)
                print(f"Q: {q}\n   {json.dumps(pl, ensure_ascii=False)}\n")
            except Exception as e:
                print(f"Q: {q}\n   [ERROR] {e}\n")
