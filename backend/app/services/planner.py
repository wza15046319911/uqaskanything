"""
planner.py — stage five: natural language -> Query Plan
Builds on query.py: adds one more program mode (course <-> program), and makes the
backend pluggable (default local Ollama qwen2.5-coder, can switch to DeepSeek).

Division of work (deterministic decisions in code, language tasks to the model):
  - LLM only does language work: judge mode, fill the structured filters slots, give an English semantic_query, extract course_code/program_name/direction
  - Code does the deterministic work: live schema injection, JSON parsing, per-key enum/type validation of filters, fixup fallback for missing fields
    (WHERE is assembled with parameters by retrieval.build_where from the validated slots, the LLM never writes SQL)

Public interface:
  - build_schema_doc(conn) -> str
  - plan(question, schema_doc=None, conn=None) -> dict
    returns {mode, filters, semantic_query, course_code, program_name, direction}
    mode ∈ {filter, semantic, hybrid, program, kb, course_detail}

Backend switch: see the llm module. If DEEPSEEK_API_KEY is set (can be put in .env) it uses DeepSeek all the way, otherwise local Ollama.

Usage:
    python planner.py "CS有哪些课程没有考试"
"""
from __future__ import annotations
import os
import re
import json
import argparse

import psycopg
from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator, model_validator

from app.services import llm

from app.core.config import DSN

LOWCARD = ["semester", "location", "attendance_mode", "level"]  # low-cardinality columns, enum values fetched live

# Real enum sets fetched by build_schema_doc, cached at module level for deterministic fallback validation (column name -> set of lowercase values).
_ENUM_CACHE: dict[str, set[str]] = {}

# Explicit degree-name signal: only allow program_to_courses when these appear (user clearly gave a degree name).
PROGRAM_NAME_RE = re.compile(
    r"(bachelors?\s+of|masters?\s+of|diploma|graduate\s+certificate|graduate\s+diploma|doctors?\s+of|"
    r"学士|硕士|博士|本科专业|研究生专业|文凭)", re.I)

# Units extraction: '2学分' / '2 units' etc., used to deterministically recover structured conditions after a wrong program route is undone.
UNITS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:学分|units?)", re.I)

# Course-type / requirement keywords: when these appear the question is "which (core/compulsory/elective) courses does a program require".
REQ_KW_RE = re.compile(
    r"核心课|核心|必修|选修|compulsory|(?<![A-Za-z])core(?![A-Za-z])|"
    r"(?<![A-Za-z])elective(?![A-Za-z])|要修哪些|有哪些课|哪些课|培养方案|课表", re.I)
# Course-program relation keywords: course code + these = "which programs is this course a compulsory/elective in".
PROG_REL_KW_RE = re.compile(r"专业|program|major|主修|培养方案|必修|选修|核心", re.I)
# Permit keywords: course code + degree name + these = "can a program take a course" (banned-course query).
PERMIT_KW_RE = re.compile(r"能修|能不能|可以修|可不可以|不能修|禁修|不可修|can\s+(?:i\s+)?take|allowed to take", re.I)
# Degree full-name extraction (English): fed to find_program's ILIKE, allows letters/spaces/slashes/&/parens/hyphens.
_PROG_NAME_EXTRACT = re.compile(
    r"((?:bachelors?|masters?|graduate\s+diploma|graduate\s+certificate|diploma|doctor)"
    r"\s+(?:of\s+)?[A-Za-z][A-Za-z /&()'\-]*)", re.I)
# Academic-level keywords -> deterministic level literal (rule 12: same input always maps the same way).
# Standalone master/bachelor/硕士/学士 also count as a level (e.g. "Master 没考试的课");
# but "Master of X" is a program name (caught first by PROGRAM_NAME_RE + _force_program_route, never reaches here),
# so master/bachelor use (?!\s+of) to exclude the "X of Y" form, avoiding mistaking a program name for a level.
# Note: level has only the two values Undergraduate / Postgraduate Coursework, master≈postgraduate (incl. certificate/diploma) is an approximation.
_LEVEL_KW = [
    (re.compile(r"研究生|postgraduate|post-graduate", re.I), "Postgraduate Coursework"),
    (re.compile(r"本科生?|undergraduate|under-graduate", re.I), "Undergraduate"),
    (re.compile(r"硕士|(?<![A-Za-z])masters?(?![A-Za-z])(?!\s+of)", re.I), "Postgraduate Coursework"),
    (re.compile(r"学士|(?<![A-Za-z])bachelors?(?![A-Za-z])(?!\s+of)", re.I), "Undergraduate"),
]

# Semester intent -> 'S1'/'S2' (deterministic). S1 uses the semester column, S2 uses S2_CODES (the column's S2 is incomplete).
_SEM_S1_RE = re.compile(r"(?<![A-Za-z])s1(?![A-Za-z])|第一学期|学期一|semester\s*1|sem\s*1", re.I)
_SEM_S2_RE = re.compile(r"(?<![A-Za-z])s2(?![A-Za-z])|第二学期|学期二|semester\s*2|sem\s*2", re.I)
# "both semesters" universal quantifier: S1 and S2 both appear + "都/both/两个学期" -> cross-semester conjunction (not a union).
# See _both_semesters_intent / retrieval.filter_search_both_semesters.
_BOTH_QUANT_RE = re.compile(r"都|两个?学期|每(?:个|学期)|both", re.I)

# The first digit of a course code = course level number (1=intro undergrad … 7/8/9=postgrad). The level column has only UG/PG,
# cannot tell 1xxx from 3xxx, so this is the only dimension to filter by "year level". The code text column never enters where (to prevent subject LIKE),
# instead it is extracted into a deterministic digit set, and qa post-filters on the first digit in Python. Supports "X(或/和/、)Y 开头/字头/打头/年级",
# "Xxxx", and English starting with X.
_CODE_LEVEL_BIND = re.compile(
    r"([1-9](?:\s*[或和、,/]\s*[1-9])*)\s*(?:开头|字头|打头|字班|年级)", re.I)
# Leading form: "开头/字头/首位/起始(为/是/:)X(或/和/、)Y" (the digit comes after the keyword, e.g. "开头为1或2或3").
_CODE_LEVEL_BIND_PRE = re.compile(
    r"(?:开头|字头|打头|首位|起始)\s*(?:数字)?\s*(?:为|是|=|:|:)?\s*"
    r"([1-9](?:\s*[或和、,/]\s*[1-9])*)", re.I)
_CODE_LEVEL_XXX = re.compile(r"([1-9])xxx", re.I)
_CODE_LEVEL_EN = re.compile(
    r"start(?:s|ing)?\s+with\s+([1-9](?:\s*(?:or|and|,)\s*[1-9])*)", re.I)

# Low-burden / "lie flat" intent (deterministic): subjective difficulty has no data, only mapped to objective burden (no exam + no hurdle + few assessment items).
# Only catches explicit "find an easy course" phrasing, not a bare "简单/容易" (easy to mix with other intents); "assessment/考核 组成简单"
# is only caught when anchored on the assessment/考核 word (= few assessment items), also landing on the objective sort by ascending assessment count.
_LOW_BURDEN_RE = re.compile(
    r"躺平|水课|划水|好过|容易过|考核少|作业少|考试少|负担轻|轻松.{0,3}课|课.{0,3}轻松|"
    r"(?:assessment|考核|考评|评估)\s*(?:组成|构成|结构|安排)?\s*(?:最)?简单", re.I)

# Has/no-exam intent (deterministic). Check negation first: "没有考试" contains the "有考试" substring, negation wins.
_EXAM_NEG_RE = re.compile(r"(没有?|无|不|without|no)\s*(期末|期终|final\s*)?(考试|exam)", re.I)
_EXAM_POS_RE = re.compile(r"(有|要|含|with)\s*(期末|期终|final\s*)?(考试|exam)", re.I)

# Has/no-group-assessment intent (deterministic, negation wins like exam). Keywords cover Chinese and English: 小组/团队作业, group work/
# groupwork/group project/group assessment, bare group/team.
_GROUP_KW = (r"(?:小组|团队|group\s*work|group\s*project|group\s*assessment|"
             r"groupwork|\bgroup\b|\bteam\b)")
_GROUP_NEG_RE = re.compile(r"(没有?|无|不|without|no)\s*" + _GROUP_KW, re.I)
_GROUP_POS_RE = re.compile(r"(有|要|含|with)\s*" + _GROUP_KW, re.I)

# Course-type exclusion intent: exclusion trigger word + type word -> course_type NOT IN (...).
# 研究(?!生) avoids mistaking "研究生" (postgraduate) for the research type.
_EXCLUDE_TRIGGER_RE = re.compile(r"排除|不含|不包括|不要|除去|去掉|剔除|except|exclud|without", re.I)
_TYPE_TOKEN_RE = [
    (re.compile(r"thesis|论文", re.I), "thesis"),
    (re.compile(r"research|研究(?!生)", re.I), "research"),
    (re.compile(r"placement|实习|practicum|internship", re.I), "placement"),
]

# Course-nature exclusion (deterministic): exclusion trigger word + these "nature words" -> controlled NOT ILIKE exclusion on title.
# Complements course_type (thesis/research/placement) — capstone/project/review/proposal etc. are title
# signals that the course_type column cannot tell apart (all fall under coursework). Each item is (detection regex, list of title substrings fed to a parameterized ILIKE);
# values are controlled on the code side, injection-safe. research/thesis/placement are also covered by the course_type NOT IN above.
_EXCLUDE_TITLE_KW = [
    (re.compile(r"capstone|顶点", re.I), ["capstone"]),
    (re.compile(r"literature\s+review|文献\s*综述", re.I), ["literature review"]),
    (re.compile(r"(?<![A-Za-z])review(?![A-Za-z])|综述", re.I), ["review"]),
    (re.compile(r"(?<![A-Za-z])project(?![A-Za-z])|项目|课程?设计", re.I), ["project"]),
    (re.compile(r"proposal|开题", re.I), ["proposal"]),
    (re.compile(r"dissertation|学位论文", re.I), ["dissertation"]),
    (re.compile(r"(?<![A-Za-z])thesis(?![A-Za-z])|毕业论文", re.I), ["thesis"]),
    (re.compile(r"(?<![A-Za-z])research(?![A-Za-z])|研究(?!生)", re.I), ["research"]),
    (re.compile(r"industry\s+placement|placement|实习|internship|practicum|实训", re.I),
     ["placement", "internship", "practicum"]),
]

# Faculty/subject grouping -> controlled coordinating_unit mapping (deterministic lookup table, text column does not enter the LLM where).
# This is a curated approximate grouping, add/remove as needed; uses parameterized SQL, injection-safe.
# Note: subject words like 计算机/CS/IT/软件 are "not" hard-locked to a faculty here. CS courses are hosted across many faculties (COSC in
# Mathematics & Physics, CYBR in Business, DATA in Historical & Philosophical Inq, etc.),
# hard-locking EECS would wrongly kill these courses (e.g. "计算机相关、没有hurdle的研究生课" would return empty). Computing-type subjects
# are always handled by semantic recall; only when the user "explicitly names a faculty" does it go through Option C (_validate_coord_unit) hard-lock.
_FACULTY_UNITS = {
    "business": ["Business School", "Economics School"],
    "arts": ["Communication & Arts School", "Languages & Cultures School",
             "Historical & Philosophical Inq", "Music School",
             "Humanities, Arts and Social Sciences", "Politic Sc & Internat Studies"],
}
_FACULTY_KW = [
    (re.compile(r"商科|商学院?|business|commerce|econ(?:omic)?", re.I), "business"),
    (re.compile(r"文科|人文|humanities|liberal\s*arts|(?<![A-Za-z])arts(?![A-Za-z])", re.I), "arts"),
]

MODES = ("filter", "semantic", "hybrid", "program", "kb", "course_detail", "guide")

# 攻略经验意图(确定性,规则 12):课程码 + 这些词 = 想问「主观经验/避坑/怎么准备」,走 mode=guide。
_GUIDE_INTENT = re.compile(r"难不难|好过吗|水不水|怎么样|体验|值不值|踩坑|避坑|怎么准备|给点建议|经验|攻略|心得", re.I)
# 事实意图(日期/先修/考核占比/Hurdle/学分):优先级高于 guide —— 同一句命中这些就走 course_detail/kb,绝不进攻略(student-facing 红线 1/3:事实问题永不召回攻略)。
_FACT_INTENT = re.compile(
    r"什么时候|何时|哪天|几号|日期|开学|开课|放假|截止|"
    r"先修|先决|前置|前导|修读要求|"
    r"考核|考评|评估|评分|成绩构成|占比|权重|考试|"
    r"学分|"
    r"\b(?:census|deadline|when|start\s*date|prerequisite|prereq|exam|assessment|weight|hurdle|units?)\b",
    re.I)

# Course code: 4 letters + 4 digits. When Chinese is adjacent the ASCII \b fails, so use lookaround boundaries;
# forbid a trailing letter/digit to avoid mistaking CSSE10012 (5 digits) for CSSE1001.
COURSE_CODE_RE = re.compile(r"(?<![A-Za-z])([A-Za-z]{4}\d{4})(?![A-Za-z0-9])")

# Abbreviation -> English subject, used to fill semantic_query.
# Boundary uses (?<![A-Za-z])X(?![A-Za-z]): when Chinese is adjacent to the abbreviation (e.g. "CS有") ASCII \b fails, so use lookaround.
ABBR = {"cs": "computer science", "ai": "artificial intelligence", "ml": "machine learning",
        "it": "information technology", "ee": "electrical engineering"}
_ABBR_RE = {a: re.compile(rf"(?<![A-Za-z]){a}(?![A-Za-z])", re.I) for a in ABBR}

# Topic-word detection: when these appear (subject/abbreviation/"相关/about") the question has a fuzzy topic and must have a semantic_query
TOPIC_HINT = re.compile(
    r"(相关|有关|关于|方向|领域|主题|about|related|topic|"
    r"计算机|软件|人工智能|机器学习|深度学习|数据|网络安全|信息安全|金融|会计|经济|"
    r"心理|生物|化学|物理|数学|统计|电子|电气|通信|机械|土木|商科|管理|市场|营销|"
    r"写作|护理|艺术|法律|医学|教育|建筑|环境|机器人|"
    r"(?<![A-Za-z])(cs|ai|ml|it|ee)(?![A-Za-z])|"
    r"computer|software|machine[\s-]*learning|"
    r"data|security|finance|account|psycholog|biolog|chemis|physic|statistic|"
    r"electric|mechanic|civil|business|market|"
    r"writing|nursing|\bart\b|\blaw\b|medic|education|architect|environment|robotic|engineer)", re.I)


PROMPT = """你是 UQ 课程库查询规划器。把用户问题转成 JSON 查询计划,只输出 JSON,不要解释。
{schema}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【第一步:判 mode(6 选 1)】
判定顺序:先看有没有课程码/学位名(→ program 或 course_detail),再看是不是学校事务(→ kb),
最后才在课程检索三类(filter/semantic/hybrid)里分。

- "course_detail":问题里出现一个课程码(如 CSSE1001),且问的是**这门课本身**
  (介绍/先修/考核/学分/什么时候开)。填 course_code,其它全空。
- "program":问的是**课程 ↔ 专业的关系**,三种 direction:
    · "course_to_programs":课程码 +「是哪些专业的必修/选修」。填 course_code。
    · "program_to_courses":学位名(Bachelor of…/Master of…/学士/硕士)+「要修哪些课/培养方案」。填 program_name。
    · "permit":课程码 + 学位名 +「能不能修/可不可以修/禁不禁修」。填 course_code + program_name。
- "kb":学校事务/政策/日期/服务,**与具体课程或专业无关**。例如开学/census/缴费/退课截止日期、
  重置密码、申请缓考、假期开放时间、停车收费、求助、开在读证明。填 kb_query(英文官方术语,
  如缓考=deferred exam、在读证明=enrolment verification letter、退课=withdraw/drop)。
  **铁律:只要出现课程码或学位名,就绝不是 kb。**
- 其余都是「在课程库里筛课」,按有没有「模糊主题」分三类:
    · "filter":只有结构化条件(学期/有无考试/hurdle/本研/学分/校区/学院…),**没有**模糊主题。
       filters 填值,semantic_query 留空。
    · "semantic":只有模糊主题/学科(机器学习/网络安全…),**没有**结构化条件。
       semantic_query 填英文,filters 全留默认。
    · "hybrid":既有结构化条件,又有模糊主题。filters 和 semantic_query 都填。

「模糊主题」= 学科方向/研究领域(计算机/人工智能/金融/心理学…),无法用某个结构化列表达,
只能靠语义召回。「学院归属」(EECS/商学院)**不是**主题(见下 coord_unit)。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【第二步:严格输出这个 JSON】不涉及的筛选维度给 null,列表给 []:
{{
  "mode": "filter|semantic|hybrid|program|kb|course_detail",
  "semantic_query": "",
  "filters": {{
    "has_exam": null, "has_hurdle": null,
    "midterm_status": null, "group_status": null,
    "level": null, "units": null,
    "location": null, "attendance_mode": null, "semester": null,
    "course_type_exclude": [], "course_type_only": [],
    "coord_unit": ""
  }},
  "course_code": "", "program_name": "", "direction": "", "kb_query": ""
}}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【第三步:逐个填槽 —— 你只填值,绝不写 SQL】

# semantic_query(模糊主题)
- 学科/方向/领域一律放这里,**必须英文**。绝不要把学科塞进 filters 或课程码。
- 缩写一定翻成英文全称,**绝不因为不认识就丢掉**:CS=computer science、AI=artificial intelligence、
  ML=machine learning、IT=information technology、EE=electrical engineering。
- 没有模糊主题(纯条件筛选)时留空 ""。

# has_exam / has_hurdle(布尔三态:true / false / null)
- 「有考试」-> has_exam=true;「没有考试/无考试/不含考试」-> has_exam=false。
- 「有 hurdle」-> has_hurdle=true;「没有 hurdle」-> has_hurdle=false。
- **否定优先**:句子里同时像「有」又像「没有」时,以否定为准(「没有期末考试」整体是「无考试」,
  填 has_exam=false,绝不要因为看到「考试」就填 true)。
- 没提到就保持 null。

# midterm_status(期中考试,'has' / 'none' / null)
- 「有期中/含期中/有 in-semester exam」-> 'has';「没有期中/不含期中」-> 'none'。
- **期中和「考试」是两件事**:期中只用 midterm_status,**绝不能用 has_exam 表达期中**。
  问「期末考试」没有专用列,只能用 has_exam(期末 ≈ 有考试)。

# group_status(小组/团队评估,'has' / 'none' / null)
- 关键词:小组作业/团队作业/group project/groupwork/group assessment/group/team。
- 「有小组作业」-> 'has';「没有小组作业/不含 group」-> 'none'(同样否定优先)。

# level(只有两个合法值,别无第三)
- 本科/本科生/bachelor/undergraduate -> "Undergraduate"。
- 研究生/硕士/master/postgraduate -> "Postgraduate Coursework"。
- **绝不能**写 "Master"/"PG"/"研究生" 这类不存在的值。
- 「Master of Computer Science」是**学位名**(→ program/program_name),不是 level;
  只有「研究生的课/master 的课」这种把 master 当层级用时,才填 level。

# units(学分,数值)
- 「2 学分/2 units」-> 2。没提到 null。

# semester(学期,'S1' / 'S2' / null)
- 第一学期/semester 1/S1 -> 'S1';第二学期/semester 2/S2 -> 'S2'。
- 「两个学期都开/S1 和 S2 都…」这种**跨学期都满足**的全称量词,**semester 仍留 null**
  (交给后端的 both-semesters 合取逻辑处理,你只要别填单个学期即可)。

# course_type_exclude / course_type_only(课程类型,合法值 coursework/placement/research/thesis)
- 「排除/不含/不要某些类型」-> 放进 course_type_exclude(如 ["thesis","research","placement"])。
- 「只要某类型/仅 placement」-> 放进 course_type_only。
- **两个列表二选一**,不要同时填。都没提到就都给 []。

# location / attendance_mode(校区 / 授课模式)—— 红线,务必照抄
- **照搬用户原话的字面值,绝不替换、绝不补全、绝不翻译成别的已知值**。
- 即使用户说的值看起来不在常见枚举里(如 Gatton、Herston、Online、远程),也**原样填**。
  是否在库里由后端判定;你擅自把 Gatton 换成 St Lucia 会把全错的结果当对的返回(严重事故)。
- 没提到校区/模式就给 null。

# coord_unit(开课学院,范围限定,不是主题)
- 用户点名某学院(EECS学院/商学院/某学院的课)时,从上方注入的**真实学院清单**里
  **逐字原样**挑一个最匹配的填进去(不改写/不缩写/不翻译);清单里找不到就留 ""。
- 学院是**范围**不是主题:用户只点名学院 + 结构化条件、而**没有**真正学科主题时 ->
  semantic_query 留空、mode=filter;既点名学院**又**有真主题(如「EECS 里跟机器学习相关的」)
  -> 同时给 semantic_query,mode=hybrid。
- 没点名学院就留 ""。计算机/CS/软件 等**学科词不要**当学院填(这类课跨多个学院挂靠),走 semantic_query。

# 组合查询(专业 + 筛选)
- 「Bachelor of X 里没有考试的课」这种**专业范围内再加结构化条件**:mode=program、
  direction=program_to_courses、program_name 填学位名,**同时**把结构化条件填进 filters(如 has_exam=false)。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【例子】(filters 中未写出的键一律保持默认 null / []，省略以保持简短)

- "没有考试的课" ->
  {{"mode":"filter","semantic_query":"","filters":{{"has_exam":false}},"course_code":"","program_name":"","direction":"","kb_query":""}}
- "没有考试的研究生课" ->
  {{"mode":"filter","semantic_query":"","filters":{{"has_exam":false,"level":"Postgraduate Coursework"}},"course_code":"","program_name":"","direction":"","kb_query":""}}
- "Master 没考试的课" ->
  {{"mode":"filter","semantic_query":"","filters":{{"has_exam":false,"level":"Postgraduate Coursework"}},"course_code":"","program_name":"","direction":"","kb_query":""}}
- "哪些课没有期中考试" ->
  {{"mode":"filter","semantic_query":"","filters":{{"midterm_status":"none"}},"course_code":"","program_name":"","direction":"","kb_query":""}}
- "有期中考试的课" ->
  {{"mode":"filter","semantic_query":"","filters":{{"midterm_status":"has"}},"course_code":"","program_name":"","direction":"","kb_query":""}}
- "没有小组作业的研究生课" ->
  {{"mode":"filter","semantic_query":"","filters":{{"group_status":"none","level":"Postgraduate Coursework"}},"course_code":"","program_name":"","direction":"","kb_query":""}}
- "没考试的、不含 placement/thesis/research 的课" ->
  {{"mode":"filter","semantic_query":"","filters":{{"has_exam":false,"course_type_exclude":["placement","thesis","research"]}},"course_code":"","program_name":"","direction":"","kb_query":""}}
- "St Lucia 校区 2 学分的本科课" ->
  {{"mode":"filter","semantic_query":"","filters":{{"location":"St Lucia","units":2,"level":"Undergraduate"}},"course_code":"","program_name":"","direction":"","kb_query":""}}
- "Gatton 校区的本科课" ->
  {{"mode":"filter","semantic_query":"","filters":{{"location":"Gatton","level":"Undergraduate"}},"course_code":"","program_name":"","direction":"","kb_query":""}}
- "线上的课" ->
  {{"mode":"filter","semantic_query":"","filters":{{"attendance_mode":"Online"}},"course_code":"","program_name":"","direction":"","kb_query":""}}
- "EECS学院下所有没考试的课" ->
  {{"mode":"filter","semantic_query":"","filters":{{"has_exam":false,"coord_unit":"Elec Engineering & Comp Science School"}},"course_code":"","program_name":"","direction":"","kb_query":""}}
- "商学院里有期中考试的课" ->
  {{"mode":"filter","semantic_query":"","filters":{{"midterm_status":"has","coord_unit":"Business School"}},"course_code":"","program_name":"","direction":"","kb_query":""}}
- "找跟机器学习相关的课" ->
  {{"mode":"semantic","semantic_query":"machine learning","filters":{{}},"course_code":"","program_name":"","direction":"","kb_query":""}}
- "CS有哪些课没有考试" ->
  {{"mode":"hybrid","semantic_query":"computer science","filters":{{"has_exam":false}},"course_code":"","program_name":"","direction":"","kb_query":""}}
- "计算机相关、没有 hurdle 的研究生课" ->
  {{"mode":"hybrid","semantic_query":"computer science","filters":{{"has_hurdle":false,"level":"Postgraduate Coursework"}},"course_code":"","program_name":"","direction":"","kb_query":""}}
- "EECS学院里跟机器学习相关的课" ->
  {{"mode":"hybrid","semantic_query":"machine learning","filters":{{"coord_unit":"Elec Engineering & Comp Science School"}},"course_code":"","program_name":"","direction":"","kb_query":""}}
- "CSSE1001 是哪些专业的必修" ->
  {{"mode":"program","semantic_query":"","filters":{{}},"course_code":"CSSE1001","program_name":"","direction":"course_to_programs","kb_query":""}}
- "Bachelor of Computer Science 要修哪些课" ->
  {{"mode":"program","semantic_query":"","filters":{{}},"course_code":"","program_name":"Bachelor of Computer Science","direction":"program_to_courses","kb_query":""}}
- "Bachelor of Computer Science 里没有考试的课" ->
  {{"mode":"program","semantic_query":"","filters":{{"has_exam":false}},"course_code":"","program_name":"Bachelor of Computer Science","direction":"program_to_courses","kb_query":""}}
- "Master of Data Science 能不能修 CSSE1001" ->
  {{"mode":"program","semantic_query":"","filters":{{}},"course_code":"CSSE1001","program_name":"Master of Data Science","direction":"permit","kb_query":""}}
- "CSSE1001 这门课讲什么 / 先修是什么" ->
  {{"mode":"course_detail","semantic_query":"","filters":{{}},"course_code":"CSSE1001","program_name":"","direction":"","kb_query":""}}
- "census date 是什么时候" ->
  {{"mode":"kb","semantic_query":"","filters":{{}},"course_code":"","program_name":"","direction":"","kb_query":"When is the census date"}}
- "怎么申请缓考" ->
  {{"mode":"kb","semantic_query":"","filters":{{}},"course_code":"","program_name":"","direction":"","kb_query":"How to apply for a deferred exam"}}

用户问题:{q}"""


def build_schema_doc(conn) -> str:
    """Fetch the distinct values of low-cardinality enum columns live and inject them, so the LLM writes WHERE with real enums."""
    enums = {c: [r[0] for r in conn.execute(
        f"SELECT DISTINCT {c} FROM courses WHERE {c} IS NOT NULL ORDER BY 1")]
        for c in LOWCARD}
    # Write the real enum sets into the module cache, for plan() to deterministically check "the LLM did not swap values on its own".
    _ENUM_CACHE.update({c: {str(v).strip().lower() for v in vals} for c, vals in enums.items()})
    # coordinating_unit is a medium-cardinality text column (about 31 faculties), not in the where whitelist; inject the real list so the LLM
    # "picks from a closed set" rather than free-generating (faculty names are all UQ internal abbreviations, free generation will misspell -> exact IN hits 0).
    coord_units = [r[0] for r in conn.execute(
        "SELECT DISTINCT coordinating_unit FROM courses "
        "WHERE coordinating_unit IS NOT NULL ORDER BY 1")]
    _ENUM_CACHE["coordinating_unit"] = {str(v).strip().lower() for v in coord_units}
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
  course_type TEXT       课程类型,实际值:['coursework', 'placement', 'research', 'thesis']
                         (普通授课课=coursework;实习/论文/研究类课程用对应值,可用 IN / NOT IN)
  midterm_status TEXT    是否含期中考试,实际值:['has', 'none', 'unknown']
                         (has=确有期中/in-semester;none=只有期末或无考试;unknown=考试命名判不出时点)
  group_status TEXT      是否含小组/团队评估,实际值:['has', 'none', 'unknown']
                         (has=有 group/team 考核;none=有考核但无 group;unknown=无考核数据判不出)
  coordinating_unit TEXT 开课学院(文本列,严禁进 where;点名学院时只能从下面清单逐字原样选填 coord_unit):
    {coord_units}
  (description / learning_outcomes / topics 等文本不在结构化列里,模糊主题要走 semantic_query)
表 programs(专业):program_id, title, total_units, rules
表 program_course(专业-课程扁平):program_id, course_code, requirement_type('core'|'elective')
  -> 课程<->专业关系问题走 mode='program'。"""


# ---------- LLM backend (pluggable) ----------

def _call_llm(prompt: str) -> str:
    """Single user prompt -> the JSON string returned by the LLM. The backend (local Ollama / DeepSeek) is chosen by the llm module by env."""
    return llm.call([{"role": "user", "content": prompt}], json_mode=True)


# ---------- deterministic validation / fallback ----------

# Closed set of legal values for filters slot validation (deterministic, shared by _FiltersModel's validators).
_TRISTATE_VALS = {"has", "none", "unknown"}
_COURSE_TYPE_VALS = {"coursework", "placement", "research", "thesis"}
_DIGITS = {"1", "2", "3", "4", "5", "6", "7", "8", "9"}


def _as_number(val):
    """Numeric slot normalization: bool is not a number; int/float as-is; numeric string to float (integer values collapse to int); otherwise None."""
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return val
    if isinstance(val, str):
        try:
            f = float(val.strip())
        except ValueError:
            return None
        return int(f) if f.is_integer() else f
    return None


class _FiltersModel(BaseModel):
    """pydantic v2 validation model for the LLM slots (single source of truth; adding one structured filter dimension = adding one field + one validator).

    Every field uses a mode="before" validator to replicate the deterministic semantics field by field, never relying on pydantic's default coerce:
    booleans do not accept "false"/1; units uses _as_number so it does not hard-cast True/strings, and declares int|float so 2 is not swallowed into 2.0;
    level is validated against the real DB enum (_ENUM_CACHE); location/attendance_mode copy the user's original value without enum validation
    (Gatton/Online red line — being off-enum is intentional, correctly empty is decided by _enforce_enum_guard/_enforce_attendance_guard).
    An illegal single field only drops that field (validator returns None), never raises; unknown keys are logged then dropped by model_validator (rule 19)."""

    model_config = ConfigDict(extra="ignore")

    has_exam: bool | None = None
    has_hurdle: bool | None = None
    midterm_status: str | None = None
    group_status: str | None = None
    level: str | None = None
    units: int | float | None = None
    location: str | None = None
    attendance_mode: str | None = None
    semester: str | None = None
    course_type_exclude: list | None = None
    course_type_only: list | None = None
    code_level: list | None = None

    @model_validator(mode="before")
    @classmethod
    def _log_unknown(cls, data):
        if isinstance(data, dict):
            known = cls.model_fields.keys()
            for k, v in data.items():
                if k not in known and v not in (None, "", [], {}):
                    print(f"[planner] 丢弃未知 filters 键:{k!r}={v!r}")
        return data

    @field_validator("has_exam", "has_hurdle", mode="before")
    @classmethod
    def _v_bool(cls, v, info: ValidationInfo):
        if isinstance(v, bool):
            return v
        if v is not None:
            print(f"[planner] 丢弃非布尔 {info.field_name}={v!r}")
        return None

    @field_validator("midterm_status", "group_status", mode="before")
    @classmethod
    def _v_tristate(cls, v, info: ValidationInfo):
        if isinstance(v, str) and v.strip().lower() in _TRISTATE_VALS:
            return v.strip().lower()
        if v is not None:
            print(f"[planner] 丢弃非法 {info.field_name}={v!r}")
        return None

    @field_validator("semester", mode="before")
    @classmethod
    def _v_semester(cls, v, info: ValidationInfo):
        if isinstance(v, str) and v.strip() in {"S1", "S2"}:
            return v.strip()
        if v is not None:
            print(f"[planner] 丢弃非法 {info.field_name}={v!r}")
        return None

    @field_validator("level", mode="before")
    @classmethod
    def _v_level(cls, v, info: ValidationInfo):
        if isinstance(v, str) and v.strip().lower() in _ENUM_CACHE.get("level", set()):
            return v.strip()
        if v is not None:
            print(f"[planner] 丢弃不在真实枚举内的 {info.field_name}={v!r}")
        return None

    @field_validator("units", mode="before")
    @classmethod
    def _v_units(cls, v, info: ValidationInfo):
        num = _as_number(v)
        if num is not None:
            return num
        if v is not None:
            print(f"[planner] 丢弃非数值 {info.field_name}={v!r}")
        return None

    @field_validator("location", "attendance_mode", mode="before")
    @classmethod
    def _v_literal(cls, v, info: ValidationInfo):
        if isinstance(v, str) and v.strip():
            return v.strip()
        if v is not None:
            print(f"[planner] 丢弃非法 {info.field_name}={v!r}")
        return None

    @field_validator("course_type_exclude", "course_type_only", mode="before")
    @classmethod
    def _v_type_list(cls, v, info: ValidationInfo):
        if not isinstance(v, list):
            if v not in (None, ""):
                print(f"[planner] 丢弃非列表 {info.field_name}={v!r}")
            return None
        clean = [t.strip().lower() for t in v
                 if isinstance(t, str) and t.strip().lower() in _COURSE_TYPE_VALS]
        dropped = [t for t in v
                   if not (isinstance(t, str) and t.strip().lower() in _COURSE_TYPE_VALS)]
        if dropped:
            print(f"[planner] 丢弃非法 course_type 值 {dropped!r}(键 {info.field_name})")
        return sorted(dict.fromkeys(clean)) or None

    @field_validator("code_level", mode="before")
    @classmethod
    def _v_digit_list(cls, v, info: ValidationInfo):
        if not isinstance(v, list):
            if v not in (None, ""):
                print(f"[planner] 丢弃非列表 {info.field_name}={v!r}")
            return None
        clean = sorted({d for d in (str(x).strip() for x in v) if d in _DIGITS})
        return clean or None


def _validate_filters(raw: dict | None) -> dict:
    """LLM slots -> validated deterministic slot dict. Internally goes through _FiltersModel (pydantic v2) for per-field validation:
    illegal values are dropped and logged (rule 19: no silent coerce, no pass-through), empty/default dimensions do not appear in the returned dict (= that dimension is not filtered).
    Keeps a dict-in / dict-out boundary, so downstream build_where, _enforce_*, qa all consume a plain dict and are unaware of pydantic."""
    if not isinstance(raw, dict):
        return {}
    return _FiltersModel.model_validate(raw).model_dump(exclude_none=True)


def _has_topic(question: str) -> bool:
    return bool(TOPIC_HINT.search(question))


# UQ known-campus whitelist (deterministic lookup table), original wording -> canonical location literal.
# When the user asks about a non-St Lucia campus that is not in the DB, it must return empty, never be swapped by the LLM to St Lucia.
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


def _enforce_enum_guard(filters: dict, question: str) -> dict:
    """Deterministic fallback: when the question mentions a campus, ensure the filters' location is not missed or altered by the LLM.

    - Campus not in the real enum (Gatton/Herston…): force location to "the user's original campus literal"
      (so SQL hits 0), never leave it as St Lucia. Core invariant: asking a non-St Lucia campus must return empty.
    - Campus in the enum (St Lucia): respect the location the LLM wrote; if missed, deterministically add it back — otherwise
      "St Lucia 校区的人工智能课" would lose the campus filter and degrade into a full-library semantic search.
    """
    loc_enum = _ENUM_CACHE.get("location", set())
    # Detect the campus mentioned in the question (deterministic table lookup), take the first matching canonical literal
    asked = None
    for key, literal in _CAMPUS_LITERALS.items():
        rx = _CAMPUS_RE.get(key)
        if rx is not None:
            if rx.search(question):
                asked = literal
                break
        elif key in question:  # non-ASCII (Chinese) direct substring match
            asked = literal
            break
    if asked is None:
        return filters
    if asked.lower() in loc_enum:
        # In the enum: pass through if the LLM wrote location, add it back if missed (setdefault does not overwrite a value the LLM got right)
        filters.setdefault("location", asked)
        return filters
    filters["location"] = asked   # not in the enum: force the user's original campus literal (so the result is correctly empty)
    return filters


# Attendance-mode intent -> canonical attendance_mode literal (deterministic lookup table). The DB's attendance_mode currently has only
# 'In Person'; when asking non-enum modes like "线上/远程" it must never be swapped by the LLM to 'In Person' (otherwise it would return all in-person
# courses as online courses, confidently wrong, hitting red line 3). Same invariant as the location guard: ask a non-enum value, use the original literal.
_ATTEND_LITERALS = {
    "in person": "In Person", "in-person": "In Person", "面授": "In Person", "线下": "In Person",
    "online": "Online", "线上": "Online", "网课": "Online",
    "external": "External", "远程": "External", "函授": "External",
}
_ATTEND_RE = {key: re.compile(rf"(?<![A-Za-z]){re.escape(key)}(?![A-Za-z])", re.I)
              for key in _ATTEND_LITERALS if key.isascii()}


def _enforce_attendance_guard(filters: dict, question: str) -> dict:
    """Deterministic fallback: when an attendance mode (online/in-person/remote…) is asked, ensure the filters' attendance_mode is not
    swapped by the LLM to the only enum value 'In Person'. Non-enum mode -> use the user's original mode literal (so the result is correctly empty, no reverse hit)."""
    asked = None
    for key, literal in _ATTEND_LITERALS.items():
        rx = _ATTEND_RE.get(key)
        if rx is not None:
            if rx.search(question):
                asked = literal
                break
        elif key in question:
            asked = literal
            break
    if asked is None:
        return filters
    enum = _ENUM_CACHE.get("attendance_mode", set())
    if asked.lower() in enum:
        filters.setdefault("attendance_mode", asked)
        return filters
    filters["attendance_mode"] = asked
    return filters


def _fallback_semantic(question: str) -> str:
    """When the question clearly has a topic but the LLM did not give a semantic_query, deterministically add one English subject word."""
    for abbr, full in ABBR.items():
        if _ABBR_RE[abbr].search(question):
            return full
    # If no abbreviation is detected, fall back to the whole sentence (bge-m3 is multilingual, can search Chinese too), hand it to the vector layer
    return question.strip()


def _extract_program_name(question: str) -> str:
    """Pull the English degree full name out of the question (the ILIKE substring fed to find_program)."""
    m = _PROG_NAME_EXTRACT.search(question)
    return m.group(1).strip() if m else ""


def _code_level_digits(question: str) -> list[str]:
    """Extract the target digit set for "filter year level by the first digit of the course code" (deduped, ascending); empty list if none.
    e.g. '1或3开头的' -> ['1','3']; '3字头' -> ['3']; 'starting with 2' -> ['2']."""
    digits: set[str] = set()
    for rx in (_CODE_LEVEL_BIND, _CODE_LEVEL_BIND_PRE, _CODE_LEVEL_EN):
        for m in rx.finditer(question):
            digits.update(re.findall(r"[1-9]", m.group(1)))
    for m in _CODE_LEVEL_XXX.finditer(question):
        digits.add(m.group(1))
    return sorted(digits)


def _expand_program_abbr(name: str) -> str:
    """Deterministically expand subject abbreviations in a degree name to full forms (rule 12: fixed mapping lookup), fed to find_program's ILIKE.
    The DB title is 'Master of Computer Science', so when the user writes 'master of CS' the ILIKE cannot match;
    whole-word replace CS->computer science etc. (ILIKE is case-insensitive). Returns as-is if no abbreviation."""
    if not name:
        return name
    out = name
    for abbr, full in ABBR.items():
        out = _ABBR_RE[abbr].sub(full, out)
    return out


def _force_program_route(question: str) -> tuple[str, str, str] | None:
    """Deterministically decide whether it is a program query (rule 12, covers LLM routing jitter).
    Returns (direction, course_code, program_name) or None (no force).
      · course code + degree full name + can-take keyword -> permit (banned-course / permit query)
      · course code + program/compulsory/elective keyword  -> course_to_programs
      · degree full name + course-type keyword             -> program_to_courses
    """
    code = COURSE_CODE_RE.search(question)
    has_degree = PROGRAM_NAME_RE.search(question)
    if code and has_degree and PERMIT_KW_RE.search(question):
        return ("permit", code.group(1).upper(), _extract_program_name(question))
    if code and PROG_REL_KW_RE.search(question):
        return ("course_to_programs", code.group(1).upper(), "")
    # Degree name + (course-type/requirement word or structured filter intent) -> that program's course list; the latter allows the "program + filter" combined query,
    # where is deterministically rebuilt and kept in plan() (see _program_filter_where).
    if has_degree and (REQ_KW_RE.search(question) or _program_filter_where(question)):
        return ("program_to_courses", "", _extract_program_name(question))
    return None


def _both_semesters_intent(question: str) -> bool:
    """Deterministically decide a "both S1 and S2…" query (cross-semester conjunction): S1 and S2 both appear with a "都/both" quantifier.
    Distinct from "S1和S2的课" (union) — the latter has no quantifier and still goes through a normal IN."""
    return bool(_SEM_S1_RE.search(question) and _SEM_S2_RE.search(question)
                and _BOTH_QUANT_RE.search(question))


def _exam_intent(question: str):
    """Deterministically decide has/no-exam intent, returns True/False/None (negation wins over affirmation)."""
    if _EXAM_NEG_RE.search(question):
        return False
    if _EXAM_POS_RE.search(question):
        return True
    return None


def _group_intent(question: str):
    """Deterministically decide has/no-group-assessment intent, returns True/False/None (negation wins over affirmation)."""
    if _GROUP_NEG_RE.search(question):
        return False
    if _GROUP_POS_RE.search(question):
        return True
    return None


def _excluded_types(question: str) -> list[str]:
    """When an exclusion trigger word is present, extract the recognizable course types mentioned in the question (thesis/research/placement)."""
    if not _EXCLUDE_TRIGGER_RE.search(question):
        return []
    out = []
    for rx, val in _TYPE_TOKEN_RE:
        if rx.search(question) and val not in out:
            out.append(val)
    return sorted(out)


def _excluded_title_kw(question: str) -> list[str]:
    """When an exclusion trigger word is present, extract the "course nature" title keywords named in the question (deduped, order kept), for a parameterized NOT ILIKE.
    The course_type column cannot tell apart capstone/project/review/proposal etc., so they can only be excluded by title."""
    if not _EXCLUDE_TRIGGER_RE.search(question):
        return []
    out: list[str] = []
    for rx, subs in _EXCLUDE_TITLE_KW:
        if rx.search(question):
            for s in subs:
                if s not in out:
                    out.append(s)
    return out


def _faculty_units(question: str) -> list[str]:
    """Faculty/subject word -> coordinating_unit list (deterministic lookup table, deduped, order kept)."""
    out: list[str] = []
    for rx, key in _FACULTY_KW:
        if rx.search(question):
            for u in _FACULTY_UNITS[key]:
                if u not in out:
                    out.append(u)
    return out


def _validate_coord_unit(raw: str) -> str:
    """Option C: the coordinating_unit the LLM picked from the real faculty list, only passes if it matches the real enum exactly.

    When a faculty name the LLM free-generated (e.g. 'EECS School') does not match the real DB value ('Elec Engineering & Comp Science School'),
    an exact IN match silently hits 0; so anything not in the enum is dropped + logged (rule 19: no pass-through, no silence),
    and the scope degrades to "no faculty limit" rather than returning a wrong result. The enum set comes from the _ENUM_CACHE written by build_schema_doc."""
    v = (raw or "").strip()
    if not v:
        return ""
    if v.lower() in _ENUM_CACHE.get("coordinating_unit", set()):
        return v
    print(f"[planner] 丢弃非法 coord_unit(不在真实学院枚举内):{raw!r}")
    return ""


def _enforce_level_hint(filters: dict, question: str) -> dict:
    """Deterministically inject the level filter (rule 12): when the question has an explicit level word, the deterministic value wins.

    When 研究生/本科/master/bachelor/硕士/学士 etc. appear in the question -> force level to the matching literal
    (overriding a value the LLM may have gotten wrong, e.g. bachelor wrongly mapped to Postgraduate). When the question has no level word, respect the level
    the LLM wrote (it may have given it from other cues like honours)."""
    for rx, val in _LEVEL_KW:
        if rx.search(question):
            filters["level"] = val
            return filters
    return filters


def _program_filter_where(question: str) -> dict:
    """Deterministically rebuild from the question the stackable structured filters "within a program scope" (combined query: program + filter).

    Only takes dimensions that map cleanly to courses columns: has/no exam / has/no group assessment / units / excluded course type /
    academic level / semester(S1 or S2); fill on match, return an empty dict if none (then it degrades to a plain program_to_courses).
    Does not rely on the LLM, guarantees determinism."""
    filters: dict = {}
    ex = _exam_intent(question)
    if ex is not None:
        filters["has_exam"] = ex
    gr = _group_intent(question)
    if gr is not None:
        filters["group_status"] = "has" if gr else "none"
    mu = UNITS_RE.search(question)
    if mu:
        filters["units"] = _as_number(mu.group(1))
    types = _excluded_types(question)
    if types:
        filters["course_type_exclude"] = types
    # Single-semester restriction (S1/S2): build_where routes it to the per-code offered_s1/offered_s2 flag. Skip the "both
    # semesters" universal quantifier (the program p2c path has no both-semester conjunction; picking one semester would be wrong).
    if not _both_semesters_intent(question):
        if _SEM_S2_RE.search(question):
            filters["semester"] = "S2"
        elif _SEM_S1_RE.search(question):
            filters["semester"] = "S1"
    return _enforce_level_hint(filters, question)


def plan(question: str, schema_doc: str | None = None, conn: object | None = None) -> dict:
    """Natural language -> query plan dict.

    When schema_doc is omitted, build it live if conn is given; if neither is given, use a static schema (enum placeholders).
    Returns {mode, filters, semantic_query, course_code, program_name, direction, kb_query, ...};
    filters is the validated structured slot dict (for retrieval.build_where to assemble with parameters, replacing the old free where string);
    kb_query is non-empty only in kb mode (an English KB query, for kb_search cross-language recall), always empty in other modes.
    """
    if schema_doc is None:
        if conn is not None:
            schema_doc = build_schema_doc(conn)
        else:
            schema_doc = ("表 courses 列:semester, year, location, attendance_mode, "
                          "level('Undergraduate'|'Postgraduate Coursework'), units, "
                          "has_exam(bool), has_hurdle(bool);文本列 code/title/description 不进 where。")

    # Deterministic "low burden" intent (躺平/水课/好过…) fast path: does not rely on the LLM, short-circuits before the model call.
    # Subjective difficulty / pass rate has no data (never let the LLM make it up, red line 1), only mapped to an objective burden filter (no exam + no hurdle)
    # + sort by ascending assessment count (order=assessments_asc, qa produces the answer deterministically). Only takes over when there is no course code, no degree name, no subject topic;
    # combined subject (an easy course within CS) is left to normal routing for now.
    if (_LOW_BURDEN_RE.search(question)
            and not COURSE_CODE_RE.search(question)
            and not PROGRAM_NAME_RE.search(question)
            and not _has_topic(question)):
        # Exclude thesis/research/placement: these have few assessment items but are actually whole-semester big projects, the heaviest burden, never count as "low burden".
        base = {"has_exam": False, "has_hurdle": False,
                "course_type_exclude": ["placement", "research", "thesis"]}
        return {
            "mode": "filter",
            "filters": _enforce_level_hint(base, question),
            "semantic_query": "", "course_code": "", "program_name": "",
            "direction": "", "coord_units": [], "order": "assessments_asc"}

    # 攻略经验意图快速通道(确定性,规则 12,不调 LLM):课程码 + 经验词 + 非事实意图 + 非专业问题 -> mode=guide。
    # 事实意图优先短路:先修/考核占比/日期/Hurdle/学分等命中时不进攻略(红线:事实问题永不召回攻略,由 course_detail/kb 处理)。
    _gm = COURSE_CODE_RE.search(question)
    if (_gm and _GUIDE_INTENT.search(question)
            and not _FACT_INTENT.search(question)
            and not PROGRAM_NAME_RE.search(question)
            and not PROG_REL_KW_RE.search(question)):
        return {
            "mode": "guide", "filters": {}, "semantic_query": "",
            "course_code": _gm.group(1).upper(), "program_name": "", "direction": "",
            "coord_units": [], "order": "", "kb_query": "",
            "both_semesters": False, "exclude_title": []}

    raw = _call_llm(PROMPT.format(schema=schema_doc, q=question))
    try:
        p = json.loads(raw)
    except json.JSONDecodeError:
        # On parse failure, first pull out the first {...} and retry (the model sometimes adds explanation/markdown fences outside the JSON)
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            raise ValueError(f"LLM 返回非法 JSON:{raw!r}")
        try:
            p = json.loads(m.group(0))
        except json.JSONDecodeError as e:
            raise ValueError(f"LLM 返回非法 JSON:{raw!r}") from e

    # filters: the LLM's structured slot object (replaces the old free where string). coord_unit does not enter build_where
    # (uses the coord_units parameterized path), so pull it from the slots first and validate it separately; semester stays in filters, build_where handles it.
    raw_filters = dict(p.get("filters")) if isinstance(p.get("filters"), dict) else {}
    llm_coord_raw = str(raw_filters.pop("coord_unit", "") or "")

    # Normalize each field. filters is validated deterministically per key against the real enum/type by _validate_filters (replaces the old _clean_where).
    # semester / coord_units use parameterized SQL: semester is a build_where column; coord_units uses _coord_clause.
    out = {
        "mode": str(p.get("mode", "")).strip().lower(),
        "filters": _validate_filters(raw_filters),
        "semantic_query": str(p.get("semantic_query", "") or "").strip(),
        "course_code": str(p.get("course_code", "") or "").strip().upper(),
        "program_name": str(p.get("program_name", "") or "").strip(),
        "direction": str(p.get("direction", "") or "").strip().lower(),
        "coord_units": [],
        "order": "",
        "kb_query": "",
        "both_semesters": False,
        "exclude_title": [],
    }

    if out["mode"] not in MODES:
        raise ValueError(f"非法 mode={out['mode']!r}(原始 {p!r})")

    # Deterministic enum fallback: when the user asks a non-enum campus, force filters to use the user's original campus literal (so the result is correctly empty),
    # never let the LLM swap Gatton for St Lucia and return the whole library.
    out["filters"] = _enforce_enum_guard(out["filters"], question)
    # Same idea: when asking non-enum attendance modes like "线上/远程", it must never be swapped for the only enum value 'In Person'
    out["filters"] = _enforce_attendance_guard(out["filters"], question)
    # "Both S1 and S2…": cross-semester conjunction (a flat IN can only express a union, count inflated). Strip the single semester slot,
    # retrieval.filter_search_both_semesters fixes it by adding IN('S1','S2') + GROUP BY HAVING for a true conjunction.
    if _both_semesters_intent(question):
        out["both_semesters"] = True
        out["filters"].pop("semester", None)
    # Course-nature title exclusion (deterministic): capstone/project/review/proposal etc. are title signals the course_type column
    # cannot tell apart, so use a parameterized NOT ILIKE (applied at the qa layer, see retrieval._title_exclude_clause). Ignored in non-course modes.
    out["exclude_title"] = _excluded_title_kw(question)
    # "Filter year level by the first digit of code" deterministic extraction (the first digit of code is a structured fact, rule 12, not left to the LLM): merged into filters as the code_level
    # slot, and retrieval.build_where emits substring(code, first digit)=ANY(%s) into SQL. The program combined
    # branch resets filters, so the same _levels is injected again in that branch (below); course_detail/kb/permit will clear filters.
    _levels = _code_level_digits(question)
    if _levels:
        out["filters"]["code_level"] = _levels
    # Subject -> controlled coordinating_unit mapping (deterministic lookup table, uses parameterized SQL); on match, limit semantic recall back to this faculty.
    # Only takes subjects concentrated in a single faculty with little cross-faculty hosting (business/arts); computing types are not here (see the _FACULTY_UNITS note).
    # Non-course modes (program/kb) are ignored by qa.
    out["coord_units"] = _faculty_units(question)
    # Option C: the coord_unit the LLM picked from the real faculty list (only passes if it matches the real enum exactly) is merged into the scope,
    # covering faculty names/abbreviations the deterministic lookup missed (e.g. EECS). The two paths are unioned, the deterministic lookup still takes priority as a baseline.
    llm_coord = _validate_coord_unit(llm_coord_raw)
    if llm_coord and llm_coord not in out["coord_units"]:
        out["coord_units"].append(llm_coord)

    # Deterministic program force (rule 12): the LLM occasionally misroutes a clear "program <-> course" query to semantic/filter, fix it here.
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
        # Course code + no degree name + no "program/compulsory/elective" relation word -> single-course detail (intro/prerequisite/assessment/units)
        out["mode"] = "course_detail"
        out["course_code"] = COURSE_CODE_RE.search(question).group(1).upper()
        out["filters"] = {}
        out["semantic_query"] = ""
        out["program_name"] = ""
        out["direction"] = ""
        return out

    # kb up-front classification: pure school-affairs/policy/date/service questions go straight to the knowledge base, not into course-library logic.
    # Deterministic safeguard (rule 12): when a course code or degree full name is present it must be a course/program question, undo kb and hand back to course routing.
    if out["mode"] == "kb":
        if COURSE_CODE_RE.search(question) or PROGRAM_NAME_RE.search(question):
            out["mode"] = "semantic"
        else:
            out["filters"] = {}
            out["semantic_query"] = ""
            out["course_code"] = ""
            out["program_name"] = ""
            out["direction"] = ""
            # Cross-language boost: the KB corpus is English, a Chinese query jitters near the threshold. The English
            # kb_query the planner produces in the same call lets kb_search recall by max(sim_zh, sim_en) (without lowering the 0.62 hard threshold, letting a real question pass the threshold by the more accurate
            # English match). Only filled when kb mode is confirmed, always empty in other modes, deterministic and no leakage.
            out["kb_query"] = str(p.get("kb_query", "") or "").strip()
            return out

    if out["mode"] == "program":
        # program mode: fill in course_code / direction
        if not out["course_code"]:
            m = COURSE_CODE_RE.search(question)
            if m:
                out["course_code"] = m.group(1).upper()
        if out["direction"] not in ("course_to_programs", "program_to_courses", "permit"):
            # With a course code, default to "which programs is this course in", otherwise treat as "which courses does the program require"
            out["direction"] = "course_to_programs" if out["course_code"] else "program_to_courses"
        # permit (can a course be taken): needs both course code + degree name, return directly
        if out["direction"] == "permit":
            if not out["program_name"]:
                out["program_name"] = _extract_program_name(question)
            out["program_name"] = _expand_program_abbr(out["program_name"])
            out["filters"] = {}
            out["semantic_query"] = ""
            return out
        # Tightened trigger: program_to_courses only passes when an explicit degree string appears in the question (Bachelor of/Master of/学士/硕士…);
        # otherwise the LLM invented a program name out of nothing (the user did not say it), so undo program and go to topic.
        if out["direction"] == "program_to_courses" and not PROGRAM_NAME_RE.search(question):
            out["mode"] = ""  # mark as undone, falls to the topic/semantic fallback below
            out["course_code"] = ""
            out["program_name"] = ""
            out["direction"] = ""
            # When the LLM misjudges program it often drops structured conditions; deterministically recover units so it can go hybrid.
            if not out["filters"]:
                mu = UNITS_RE.search(question)
                if mu:
                    out["filters"] = {"units": _as_number(mu.group(1))}
        else:
            # program_to_courses can stack structured filters "within the program scope" (deterministically rebuilt from the question, not relying on the LLM);
            # course_to_programs and others have no extra conditions, clear them to avoid misuse.
            out["filters"] = (_program_filter_where(question)
                              if out["direction"] == "program_to_courses" else {})
            # code_level is also merged (this branch reset filters); only meaningful for the program_to_courses course-list scope.
            if _levels and out["direction"] == "program_to_courses":
                out["filters"]["code_level"] = _levels
            out["program_name"] = _expand_program_abbr(out["program_name"])
            out["semantic_query"] = ""
            return out

    # A course_detail reaching here is an LLM misroute: the valid course_detail returns early above
    # with a regex-validated code, so any course_detail left at this point has no real code (e.g. "介绍一下 cs se").
    # Demote it to "" so the fallback chain below re-routes by topic, instead of qa calling course_detail with an empty code (500).
    if out["mode"] == "course_detail":
        out["mode"] = ""

    # Non-program: clear program-related fields
    out["course_code"] = ""
    out["program_name"] = ""
    out["direction"] = ""

    topic = _has_topic(question)
    # Deterministic level fallback: when "研究生/本科" is asked but filters has no level, add it (rule 12), fixing an LLM-missed filter.
    out["filters"] = _enforce_level_hint(out["filters"], question)

    # Faculty = deterministic scope (coord_units), not a semantic topic. When a faculty is named + there are structured filters, and there is no real topic besides the faculty name,
    # hand the scope to coord_units with a pure filter and clear semantic_query — otherwise the LLM easily treats the faculty name (especially abbreviations like EECS,
    # which bge-m3 can barely embed) as a topic and goes hybrid, all filtered to 0 by min_sim. Only keep in-faculty semantics when there is a real topic (machine learning…).
    if out["coord_units"] and out["filters"] and not topic:
        out["semantic_query"] = ""
        out["mode"] = "filter"

    # Reverse guard (symmetric to "fallback 1" below): no real topic word (_has_topic=False) but already has structured filters,
    # yet misjudged by the LLM as semantic/hybrid — commonly a structured constraint like "code 开头为 X" stuffed into
    # semantic_query as a topic. The code prefix is already handled deterministically by the code_level slot, so clear the fake semantic_query and drop back to filter,
    # otherwise a pure filter query gets cut to a few rows by the vector min_sim gate (the non-deterministic LLM hits this occasionally, results vary).
    if not topic and out["filters"] and out["mode"] in ("semantic", "hybrid"):
        out["semantic_query"] = ""
        out["mode"] = "filter"

    # After program is undone (mode="") it needs to land on a valid mode: with filters go filter, otherwise semantic.
    if out["mode"] == "":
        out["mode"] = "filter" if out["filters"] else "semantic"
    # level-hint added filters to a semantic: with a topic upgrade to hybrid, without a topic drop to filter.
    if out["filters"] and out["mode"] == "semantic":
        out["mode"] = "hybrid" if topic else "filter"

    # Fallback 1: question has a topic but mode landed on filter -> upgrade to hybrid (with filters) or semantic (without filters)
    if topic and out["mode"] == "filter":
        out["mode"] = "hybrid" if out["filters"] else "semantic"

    # Fallback 2: semantic/hybrid is missing semantic_query and the question has a topic -> deterministically add an English subject word
    if out["mode"] in ("semantic", "hybrid") and not out["semantic_query"]:
        if topic:
            out["semantic_query"] = _fallback_semantic(question)
        elif out["mode"] == "hybrid":
            # hybrid but no topic word and cannot be filled -> fall back to filter
            out["mode"] = "filter"
        else:
            raise ValueError(f"semantic 模式缺 semantic_query 且问题无主题词:{question!r}")

    # Fallback 3: filter/hybrid must have legal filters (empty filters makes filter_search raise -> qa turns it into empty)
    if out["mode"] in ("filter", "hybrid") and not out["filters"]:
        if out["semantic_query"]:
            out["mode"] = "semantic"   # only semantics left, downgrade
        else:
            raise ValueError(f"{out['mode']} 模式无合法 filters 也无 semantic_query:{question!r}")

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
    # Self-test against the real DB + Ollama
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
