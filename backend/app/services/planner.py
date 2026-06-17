"""
planner.py — 阶段五:自然语言 -> 查询计划(Query Plan)
在 query.py 的基础上做强:多一个 program 模式(课程<->专业),并把后端做成可插拔
(默认本地 Ollama qwen2.5-coder,可切 DeepSeek)。

分工(确定性决策用代码,语言任务交模型):
  - LLM 只做语言活:判 mode、填结构化 filters 槽位、给英文 semantic_query、抽 course_code/program_name/direction
  - 代码做确定性活:schema 实时注入、JSON 解析、filters 逐键枚举/类型校验、缺失字段纠偏兜底
    (WHERE 由 retrieval.build_where 从校验后的槽位参数化拼装,LLM 绝不写 SQL)

公开接口:
  - build_schema_doc(conn) -> str
  - plan(question, schema_doc=None, conn=None) -> dict
    返回 {mode, filters, semantic_query, course_code, program_name, direction}
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
from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator, model_validator

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
# 独立的 master/bachelor/硕士/学士 也当层级用(如"Master 没考试的课");
# 但"Master of X"是 program 名(由 PROGRAM_NAME_RE + _force_program_route 先拦,跑不到这里),
# 故 master/bachelor 用 (?!\s+of) 排除"X of Y"写法,避免误把专业名当层级。
# 注意:level 只有 Undergraduate / Postgraduate Coursework 两值,master≈研究生(含证书/文凭)为近似。
_LEVEL_KW = [
    (re.compile(r"研究生|postgraduate|post-graduate", re.I), "Postgraduate Coursework"),
    (re.compile(r"本科生?|undergraduate|under-graduate", re.I), "Undergraduate"),
    (re.compile(r"硕士|(?<![A-Za-z])masters?(?![A-Za-z])(?!\s+of)", re.I), "Postgraduate Coursework"),
    (re.compile(r"学士|(?<![A-Za-z])bachelors?(?![A-Za-z])(?!\s+of)", re.I), "Undergraduate"),
]

# 学期意图 -> 'S1'/'S2'(确定性)。S1 用 semester 列,S2 走 S2_CODES(列里 S2 不全)。
_SEM_S1_RE = re.compile(r"(?<![A-Za-z])s1(?![A-Za-z])|第一学期|学期一|semester\s*1|sem\s*1", re.I)
_SEM_S2_RE = re.compile(r"(?<![A-Za-z])s2(?![A-Za-z])|第二学期|学期二|semester\s*2|sem\s*2", re.I)
# 「两学期都」全称量词:S1、S2 同时出现 + 「都/both/两个学期」-> 跨学期合取(不是并集)。
# 见 _both_semesters_intent / retrieval.filter_search_both_semesters。
_BOTH_QUANT_RE = re.compile(r"都|两个?学期|每(?:个|学期)|both", re.I)

# course code 首位数字 = 课程级别号(1=入门本科…7/8/9=研究生)。level 列只有 UG/PG,
# 区分不了 1xxx vs 3xxx,故这是唯一能按「年级」筛的维度。code 文本列绝不进 where(防学科 LIKE),
# 改抽成确定性数字集,qa 层 Python 后过滤首位数字。支持「X(或/和/、)Y 开头/字头/打头/年级」、
# 「Xxxx」、英文 starting with X。
_CODE_LEVEL_BIND = re.compile(
    r"([1-9](?:\s*[或和、,/]\s*[1-9])*)\s*(?:开头|字头|打头|字班|年级)", re.I)
# 前置写法:「开头/字头/首位/起始(为/是/:)X(或/和/、)Y」(数字在关键词之后,如「开头为1或2或3」)。
_CODE_LEVEL_BIND_PRE = re.compile(
    r"(?:开头|字头|打头|首位|起始)\s*(?:数字)?\s*(?:为|是|=|:|:)?\s*"
    r"([1-9](?:\s*[或和、,/]\s*[1-9])*)", re.I)
_CODE_LEVEL_XXX = re.compile(r"([1-9])xxx", re.I)
_CODE_LEVEL_EN = re.compile(
    r"start(?:s|ing)?\s+with\s+([1-9](?:\s*(?:or|and|,)\s*[1-9])*)", re.I)

# 低负担/「躺平」意图(确定性):主观难度无数据,只映射成客观负担(无考试+无hurdle+考核项少)。
# 仅收明确的找轻松课表述,不收裸"简单/容易"(易和别的意图混);「assessment/考核 组成简单」
# 锚定在 assessment/考核 词上才收(=考核项少),同样落到按考核项数升序的客观排序。
_LOW_BURDEN_RE = re.compile(
    r"躺平|水课|划水|好过|容易过|考核少|作业少|考试少|负担轻|轻松.{0,3}课|课.{0,3}轻松|"
    r"(?:assessment|考核|考评|评估)\s*(?:组成|构成|结构|安排)?\s*(?:最)?简单", re.I)

# 有/无考试意图(确定性)。先判否定:"没有考试"含"有考试"子串,否定优先。
_EXAM_NEG_RE = re.compile(r"(没有?|无|不|without|no)\s*(期末|期终|final\s*)?(考试|exam)", re.I)
_EXAM_POS_RE = re.compile(r"(有|要|含|with)\s*(期末|期终|final\s*)?(考试|exam)", re.I)

# 有/无小组评估意图(确定性,同 exam 否定优先)。关键词覆盖中英:小组/团队作业、group work/
# groupwork/group project/group assessment、裸 group/team。
_GROUP_KW = (r"(?:小组|团队|group\s*work|group\s*project|group\s*assessment|"
             r"groupwork|\bgroup\b|\bteam\b)")
_GROUP_NEG_RE = re.compile(r"(没有?|无|不|without|no)\s*" + _GROUP_KW, re.I)
_GROUP_POS_RE = re.compile(r"(有|要|含|with)\s*" + _GROUP_KW, re.I)

# 课程类型排除意图:出现排除触发词 + 类型词 -> course_type NOT IN (...)。
# 研究(?!生) 避免把"研究生"(postgraduate)误当 research 类型。
_EXCLUDE_TRIGGER_RE = re.compile(r"排除|不含|不包括|不要|除去|去掉|剔除|except|exclud|without", re.I)
_TYPE_TOKEN_RE = [
    (re.compile(r"thesis|论文", re.I), "thesis"),
    (re.compile(r"research|研究(?!生)", re.I), "research"),
    (re.compile(r"placement|实习|practicum|internship", re.I), "placement"),
]

# 课程性质排除(确定性):排除触发词 + 这些"性质词" -> 标题受控 NOT ILIKE 排除。
# 与 course_type(thesis/research/placement)互补——capstone/project/review/proposal 等是 title
# 信号,course_type 列分不出(都归 coursework)。每项 (识别正则, 喂参数化 ILIKE 的标题子串列表);
# 值代码侧受控,注入安全。research/thesis/placement 同时也会被上面的 course_type NOT IN 覆盖。
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

# 学院/学科分组 -> coordinating_unit 受控映射(确定性查表,文本列不进 LLM where)。
# 这是策划好的近似分组,可按需增删;走参数化 SQL,注入安全。
# 注:计算机/CS/IT/软件 等学科词「不」在此硬锁学院。CS 课大量跨学院挂靠(COSC 在
# Mathematics & Physics、CYBR 在 Business、DATA 在 Historical & Philosophical Inq 等),
# 硬锁 EECS 会误杀这些课(如「计算机相关、没有hurdle的研究生课」会返回空)。计算机类学科
# 一律靠语义召回处理;只有用户「显式点名学院」时才经 Option C(_validate_coord_unit)硬锁。
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

MODES = ("filter", "semantic", "hybrid", "program", "kb", "course_detail")

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
    """实时取低基数枚举列的 distinct 值注入,保证 LLM 用真实枚举写 WHERE。"""
    enums = {c: [r[0] for r in conn.execute(
        f"SELECT DISTINCT {c} FROM courses WHERE {c} IS NOT NULL ORDER BY 1")]
        for c in LOWCARD}
    # 真实枚举集合写入模块缓存,供 plan() 确定性校验「LLM 没擅自换值」。
    _ENUM_CACHE.update({c: {str(v).strip().lower() for v in vals} for c, vals in enums.items()})
    # coordinating_unit 是中基数文本列(约 31 个学院),不进 where 白名单;注入真实清单让 LLM
    # 「从闭集里选」而非自由生成(院名拼写都是 UQ 内部缩写,自由生成必拼错→精确 IN 命中 0)。
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


# ---------- LLM 后端(可插拔)----------

def _call_llm(prompt: str) -> str:
    """单条 user prompt -> LLM 返回的 JSON 字符串。后端(本地 Ollama / DeepSeek)由 llm 模块按 env 选。"""
    return llm.call([{"role": "user", "content": prompt}], json_mode=True)


# ---------- 确定性校验 / 兜底 ----------

# filters 槽位校验用的合法值闭集(确定性,_FiltersModel 的 validators 共用)。
_TRISTATE_VALS = {"has", "none", "unknown"}
_COURSE_TYPE_VALS = {"coursework", "placement", "research", "thesis"}
_DIGITS = {"1", "2", "3", "4", "5", "6", "7", "8", "9"}


def _as_number(val):
    """数值槽位归一化:bool 不算数值;int/float 原样;数字字符串转 float(整数值收敛成 int);否则 None。"""
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
    """LLM 槽位的 pydantic v2 校验模型(单一真相源;加一种结构化筛选维度 = 加一个字段 + 一个 validator)。

    每个字段都用 mode="before" validator 逐字段复刻确定性语义,绝不依赖 pydantic 默认 coerce:
    布尔不接受 "false"/1;units 用 _as_number 不把 True/字符串硬转,且声明 int|float 避免 2 被吞成 2.0;
    level 按真实 DB 枚举(_ENUM_CACHE)校验;location/attendance_mode 照搬用户原值、不校验枚举
    (Gatton/Online 红线——非枚举是故意的,正确为空由 _enforce_enum_guard/_enforce_attendance_guard 裁定)。
    单字段非法只丢该字段(validator 返回 None)、绝不抛;未知键由 model_validator 记日志后丢弃(规则19)。"""

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
    """LLM 槽位 -> 校验后的确定性槽位 dict。内部走 _FiltersModel(pydantic v2)逐字段校验:
    非法值丢弃并记日志(规则19:不静默 coerce、不放行),空/缺省维度不出现在返回 dict 里(= 该维度不过滤)。
    保持 dict 进 / dict 出的边界,下游 build_where、_enforce_*、qa 全部消费纯 dict,不感知 pydantic。"""
    if not isinstance(raw, dict):
        return {}
    return _FiltersModel.model_validate(raw).model_dump(exclude_none=True)


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


def _enforce_enum_guard(filters: dict, question: str) -> dict:
    """确定性兜底:问题提到某校区时,保证 filters 的 location 不被 LLM 漏写或篡改。

    - 校区不在真实枚举内(Gatton/Herston…):强制把 location 设成「用户原校区字面值」
      (使 SQL 命中 0),绝不留成 St Lucia。核心不变量:问非 St Lucia 校区必须返回空。
    - 校区在枚举内(St Lucia):LLM 已写 location 就尊重;漏写则确定性补回——否则
      「St Lucia 校区的人工智能课」会丢掉校区过滤,退化成全库语义检索。
    """
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
        return filters
    if asked.lower() in loc_enum:
        # 在枚举内:LLM 写了 location 就放行,漏写则补回(setdefault 不覆盖 LLM 写对的值)
        filters.setdefault("location", asked)
        return filters
    filters["location"] = asked   # 不在枚举内:强制用户原校区字面值(使结果正确为空)
    return filters


# 授课模式意图 -> 规范 attendance_mode 字面值(确定性查表)。库里 attendance_mode 目前只有
# 'In Person';问「线上/远程」等非枚举模式时绝不能被 LLM 换成 'In Person'(否则把全部面授课
# 当线上课返回,confidently wrong,踩红线3)。同 location 守卫的不变量:问非枚举值用原字面值。
_ATTEND_LITERALS = {
    "in person": "In Person", "in-person": "In Person", "面授": "In Person", "线下": "In Person",
    "online": "Online", "线上": "Online", "网课": "Online",
    "external": "External", "远程": "External", "函授": "External",
}
_ATTEND_RE = {key: re.compile(rf"(?<![A-Za-z]){re.escape(key)}(?![A-Za-z])", re.I)
              for key in _ATTEND_LITERALS if key.isascii()}


def _enforce_attendance_guard(filters: dict, question: str) -> dict:
    """确定性兜底:问到授课模式(线上/面授/远程…)时,保证 filters 的 attendance_mode 不被 LLM
    换成枚举里仅有的 'In Person'。非枚举模式 -> 用用户原模式字面值(使结果正确为空,不反向命中)。"""
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


def _code_level_digits(question: str) -> list[str]:
    """抽出「按 course code 首位数字筛年级」的目标数字集(去重升序);无则空列表。
    例:'1或3开头的' -> ['1','3'];'3字头' -> ['3'];'starting with 2' -> ['2']。"""
    digits: set[str] = set()
    for rx in (_CODE_LEVEL_BIND, _CODE_LEVEL_BIND_PRE, _CODE_LEVEL_EN):
        for m in rx.finditer(question):
            digits.update(re.findall(r"[1-9]", m.group(1)))
    for m in _CODE_LEVEL_XXX.finditer(question):
        digits.add(m.group(1))
    return sorted(digits)


def _expand_program_abbr(name: str) -> str:
    """确定性把学位名里的学科缩写展成全称(规则12:固定映射查表),喂 find_program 的 ILIKE。
    库里 title 是 'Master of Computer Science',用户写 'master of CS' 时 ILIKE 命中不了;
    整词替换 CS->computer science 等(ILIKE 不分大小写)。无缩写则原样返回。"""
    if not name:
        return name
    out = name
    for abbr, full in ABBR.items():
        out = _ABBR_RE[abbr].sub(full, out)
    return out


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
    # 学位名 + (课型/要求词 或 结构化筛选意图)-> 该专业课表;后者放行「专业 + 筛选」组合查询,
    # where 在 plan() 里确定性重建并保留(见 _program_filter_where)。
    if has_degree and (REQ_KW_RE.search(question) or _program_filter_where(question)):
        return ("program_to_courses", "", _extract_program_name(question))
    return None


def _both_semesters_intent(question: str) -> bool:
    """确定性判「S1 和 S2 都…」类查询(跨学期合取):S1、S2 同时出现且带「都/both」量词。
    与「S1和S2的课」(并集)区分——后者无量词,仍走普通 IN。"""
    return bool(_SEM_S1_RE.search(question) and _SEM_S2_RE.search(question)
                and _BOTH_QUANT_RE.search(question))


def _exam_intent(question: str):
    """确定性判有无考试意图,返回 True/False/None(否定优先于肯定)。"""
    if _EXAM_NEG_RE.search(question):
        return False
    if _EXAM_POS_RE.search(question):
        return True
    return None


def _group_intent(question: str):
    """确定性判有无小组评估意图,返回 True/False/None(否定优先于肯定)。"""
    if _GROUP_NEG_RE.search(question):
        return False
    if _GROUP_POS_RE.search(question):
        return True
    return None


def _excluded_types(question: str) -> list[str]:
    """有排除触发词时,抽出问题里提到的可识别课程类型(thesis/research/placement)。"""
    if not _EXCLUDE_TRIGGER_RE.search(question):
        return []
    out = []
    for rx, val in _TYPE_TOKEN_RE:
        if rx.search(question) and val not in out:
            out.append(val)
    return sorted(out)


def _excluded_title_kw(question: str) -> list[str]:
    """有排除触发词时,抽出问题里点名的「课程性质」标题关键词(去重保序),供参数化 NOT ILIKE。
    course_type 列分不出 capstone/project/review/proposal 这类,只能据 title 排除。"""
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
    """学院/学科词 -> coordinating_unit 列表(确定性查表,去重保序)。"""
    out: list[str] = []
    for rx, key in _FACULTY_KW:
        if rx.search(question):
            for u in _FACULTY_UNITS[key]:
                if u not in out:
                    out.append(u)
    return out


def _validate_coord_unit(raw: str) -> str:
    """Option C:LLM 从真实学院清单选出的 coordinating_unit,逐字命中真实枚举才放行。

    LLM 自由生成的院名(如 'EECS School')与 DB 实际值('Elec Engineering & Comp Science School')
    对不上时,精确 IN 匹配会静默命中 0;故不在枚举内一律丢弃 + 记日志(规则19:不放行、不静默),
    范围降级为不限学院,而不是返回错误结果。枚举集来自 build_schema_doc 写入的 _ENUM_CACHE。"""
    v = (raw or "").strip()
    if not v:
        return ""
    if v.lower() in _ENUM_CACHE.get("coordinating_unit", set()):
        return v
    print(f"[planner] 丢弃非法 coord_unit(不在真实学院枚举内):{raw!r}")
    return ""


def _enforce_level_hint(filters: dict, question: str) -> dict:
    """确定性注入 level 过滤(规则 12):问题含明确层级词时,确定性值为准。

    问题里出现 研究生/本科/master/bachelor/硕士/学士 等 -> 强制把 level 设成对应字面值
    (覆盖 LLM 可能写错的值,如 bachelor 被错映射成 Postgraduate)。问题无层级词时尊重 LLM
    已写的 level(可能据 honours 等其它线索给出)。"""
    for rx, val in _LEVEL_KW:
        if rx.search(question):
            filters["level"] = val
            return filters
    return filters


def _program_filter_where(question: str) -> dict:
    """确定性从问题重建「专业范围内」可叠加的结构化 filters(组合查询:专业 + 筛选)。

    只取能干净映射到 courses 列的维度:有无考试 / 有无小组评估 / 学分 / 排除课型 / 学历层级;命中即填,
    都没有则返回空 dict(此时退化为普通 program_to_courses)。不依赖 LLM,保证确定性。"""
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
    return _enforce_level_hint(filters, question)


def plan(question: str, schema_doc: str | None = None, conn: object | None = None) -> dict:
    """自然语言 -> 查询计划 dict。

    schema_doc 缺省时若给了 conn 就实时构建;两者都没有则用一份静态 schema(枚举占位)。
    返回 {mode, filters, semantic_query, course_code, program_name, direction, kb_query, ...};
    filters 是校验后的结构化槽位 dict(供 retrieval.build_where 参数化拼装,取代旧的自由 where 串);
    kb_query 仅 kb 模式非空(英文 KB query,供 kb_search 跨语言召回),其它模式恒空。
    """
    if schema_doc is None:
        if conn is not None:
            schema_doc = build_schema_doc(conn)
        else:
            schema_doc = ("表 courses 列:semester, year, location, attendance_mode, "
                          "level('Undergraduate'|'Postgraduate Coursework'), units, "
                          "has_exam(bool), has_hurdle(bool);文本列 code/title/description 不进 where。")

    # 确定性「低负担」意图(躺平/水课/好过…)快路径:不依赖 LLM,先于模型调用短路。
    # 主观难度/通过率无数据(绝不交 LLM 编,红线1),只映射成客观负担过滤(无考试+无 hurdle)
    # + 按考核项数升序(order=assessments_asc,qa 层确定性出答案)。无课码、无学位名、无学科主题
    # 时才接管;组合学科(CS 里轻松的课)暂退给常规路由。
    if (_LOW_BURDEN_RE.search(question)
            and not COURSE_CODE_RE.search(question)
            and not PROGRAM_NAME_RE.search(question)
            and not _has_topic(question)):
        # 排除 thesis/research/placement:这类考核项少但实为整学期大项目,负担最重,绝不算「低负担」。
        base = {"has_exam": False, "has_hurdle": False,
                "course_type_exclude": ["placement", "research", "thesis"]}
        return {
            "mode": "filter",
            "filters": _enforce_level_hint(base, question),
            "semantic_query": "", "course_code": "", "program_name": "",
            "direction": "", "coord_units": [], "order": "assessments_asc"}

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

    # filters:LLM 的结构化槽位对象(取代旧的自由 where 串)。coord_unit 不进 build_where
    # (走 coord_units 参数化路径),先从槽位取出再单独校验;semester 留在 filters,build_where 处理。
    raw_filters = dict(p.get("filters")) if isinstance(p.get("filters"), dict) else {}
    llm_coord_raw = str(raw_filters.pop("coord_unit", "") or "")

    # 归一化各字段。filters 由 _validate_filters 逐键按真实枚举/类型确定性校验(取代旧 _clean_where)。
    # semester / coord_units 走参数化 SQL:semester 是 build_where 的列;coord_units 走 _coord_clause。
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

    # 确定性枚举兜底:用户问非枚举校区时,强制 filters 用用户原校区字面值(使结果正确为空),
    # 绝不放任 LLM 把 Gatton 换成 St Lucia 返回全库。
    out["filters"] = _enforce_enum_guard(out["filters"], question)
    # 同理:问「线上/远程」等非枚举授课模式时,绝不能被换成枚举里仅有的 'In Person'
    out["filters"] = _enforce_attendance_guard(out["filters"], question)
    # 「S1 和 S2 都…」:跨学期合取(扁平 IN 只能表达并集,数量虚高)。剥掉单 semester 槽,
    # 由 retrieval.filter_search_both_semesters 固定补 IN('S1','S2') + GROUP BY HAVING 取真合取。
    if _both_semesters_intent(question):
        out["both_semesters"] = True
        out["filters"].pop("semester", None)
    # 课程性质标题排除(确定性):capstone/project/review/proposal 等 title 信号,course_type 列
    # 分不出,走参数化 NOT ILIKE(qa 层施加,见 retrieval._title_exclude_clause)。非课程模式忽略。
    out["exclude_title"] = _excluded_title_kw(question)
    # 「按 code 首位数字筛年级」确定性抽取(code 首位是结构化事实,rule-12,不交 LLM):作为 code_level
    # 槽位并入 filters,由 retrieval.build_where 出 substring(code,首位)=ANY(%s) 走 SQL。program 组合
    # 分支会重置 filters,故同一 _levels 在该分支再注入一次(下方);course_detail/kb/permit 会清空 filters。
    _levels = _code_level_digits(question)
    if _levels:
        out["filters"]["code_level"] = _levels
    # 学科 -> coordinating_unit 受控映射(确定性查表,走参数化 SQL);命中则把语义召回限定回本学院。
    # 只收聚在单一学院、跨学院挂靠少的学科(商科/文科);计算机类不在此(见 _FACULTY_UNITS 注)。
    # 非课程模式(program/kb)由 qa 忽略。
    out["coord_units"] = _faculty_units(question)
    # Option C:LLM 从真实学院清单选出的 coord_unit(逐字命中真实枚举才放行)并入范围,
    # 覆盖确定性查表没收的院名/缩写(如 EECS)。两路取并集,确定性查表仍优先保底。
    llm_coord = _validate_coord_unit(llm_coord_raw)
    if llm_coord and llm_coord not in out["coord_units"]:
        out["coord_units"].append(llm_coord)

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
        out["filters"] = {}
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
            out["filters"] = {}
            out["semantic_query"] = ""
            out["course_code"] = ""
            out["program_name"] = ""
            out["direction"] = ""
            # 跨语言增强:KB 语料是英文,中文 query 贴阈抖动。planner 同一次调用顺带产出的英文
            # kb_query 让 kb_search 取 max(sim_中, sim_英) 召回(不下调 0.62 硬阈值,让真问题靠更准的
            # 英文匹配过阈)。仅在确认 kb 模式时填,其它模式恒空,确定性、不泄漏。
            out["kb_query"] = str(p.get("kb_query", "") or "").strip()
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
            out["program_name"] = _expand_program_abbr(out["program_name"])
            out["filters"] = {}
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
            if not out["filters"]:
                mu = UNITS_RE.search(question)
                if mu:
                    out["filters"] = {"units": _as_number(mu.group(1))}
        else:
            # program_to_courses 可叠加「专业范围内」的结构化筛选(确定性从问题重建,不依赖 LLM);
            # course_to_programs 等无附加条件,清空避免误用。
            out["filters"] = (_program_filter_where(question)
                              if out["direction"] == "program_to_courses" else {})
            # code_level 同样并入(该分支重置了 filters);只对 program_to_courses 的课表范围有意义。
            if _levels and out["direction"] == "program_to_courses":
                out["filters"]["code_level"] = _levels
            out["program_name"] = _expand_program_abbr(out["program_name"])
            out["semantic_query"] = ""
            return out

    # 非 program:清空专业相关字段
    out["course_code"] = ""
    out["program_name"] = ""
    out["direction"] = ""

    topic = _has_topic(question)
    # 确定性 level 兜底:问"研究生/本科"但 filters 未含 level 时补上(规则 12),修 LLM 漏过滤。
    out["filters"] = _enforce_level_hint(out["filters"], question)

    # 学院 = 确定性范围(coord_units),不是语义主题。点名学院 + 有结构化 filters、且除院名外无真主题时,
    # 范围交给 coord_units 走纯 filter、清掉 semantic_query —— 否则 LLM 易把院名(尤其 EECS 这类缩写,
    # bge-m3 几乎 embed 不出)当主题走 hybrid,全被 min_sim 滤成 0。有真主题(机器学习…)才保留院内语义。
    if out["coord_units"] and out["filters"] and not topic:
        out["semantic_query"] = ""
        out["mode"] = "filter"

    # 反向守卫(对称于下面「兜底 1」):无真实主题词(_has_topic=False)却已有结构化 filters,
    # 但被 LLM 误判成 semantic/hybrid——常见是把「code 开头为 X」这类结构化约束当主题塞进
    # semantic_query。code 前缀已由 code_level 槽位确定性处理,这里清空伪 semantic_query、降回 filter,
    # 否则纯筛选查询会被向量 min_sim 门滤成寥寥几条(LLM 非确定性偶发踩到,结果时多时少)。
    if not topic and out["filters"] and out["mode"] in ("semantic", "hybrid"):
        out["semantic_query"] = ""
        out["mode"] = "filter"

    # program 被撤销(mode="")后需要落到一个有效 mode:有 filters 走 filter,否则 semantic。
    if out["mode"] == "":
        out["mode"] = "filter" if out["filters"] else "semantic"
    # level-hint 给 semantic 补了 filters:有主题升 hybrid,无主题降 filter。
    if out["filters"] and out["mode"] == "semantic":
        out["mode"] = "hybrid" if topic else "filter"

    # 兜底 1:问题含主题但 mode 落到 filter -> 升级 hybrid(有 filters)或 semantic(无 filters)
    if topic and out["mode"] == "filter":
        out["mode"] = "hybrid" if out["filters"] else "semantic"

    # 兜底 2:semantic/hybrid 缺 semantic_query 且问题含主题 -> 确定性补英文学科词
    if out["mode"] in ("semantic", "hybrid") and not out["semantic_query"]:
        if topic:
            out["semantic_query"] = _fallback_semantic(question)
        elif out["mode"] == "hybrid":
            # hybrid 却没主题词且补不出来 -> 退回 filter
            out["mode"] = "filter"
        else:
            raise ValueError(f"semantic 模式缺 semantic_query 且问题无主题词:{question!r}")

    # 兜底 3:filter/hybrid 必须有合法 filters(空 filters 会让 filter_search 抛→qa 接成 empty)
    if out["mode"] in ("filter", "hybrid") and not out["filters"]:
        if out["semantic_query"]:
            out["mode"] = "semantic"   # 只剩语义,降级
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
