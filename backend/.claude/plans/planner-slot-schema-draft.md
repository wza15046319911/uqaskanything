# Planner 槽位 schema / PROMPT 草稿(v0,待评审)

> 这是给评审看的草稿,不是最终代码。目的是先对齐「LLM 输出长什么样、代码怎么校验拼装」。

## 1. LLM 输出契约(json_mode)

```jsonc
{
  // 路由:语义不变
  "mode": "filter | semantic | hybrid | program | kb | course_detail",

  // 模糊主题(学科/方向),英文。filter/program/kb 留空
  "semantic_query": "",

  // 结构化筛选槽位:取代旧的自由 where 串。
  // 不涉及的维度一律给 null(列表给 [])。代码只认这些键,多余键忽略。
  "filters": {
    "has_exam":        true | false | null,
    "has_hurdle":      true | false | null,
    "midterm_status":  "has" | "none" | null,        // 期中;期末无专用列,用 has_exam
    "group_status":    "has" | "none" | null,         // 小组/团队评估
    "level":           "Undergraduate" | "Postgraduate Coursework" | null,
    "units":           2 | null,                      // 数值,或 null
    "location":        "St Lucia" | null,             // 用户原话校区,代码再校验枚举
    "attendance_mode": "Online" | null,               // 同上,非枚举值代码会保留→正确空
    "semester":        "S1" | "S2" | null,
    "course_type_exclude": ["thesis", "research", "placement"],  // NOT IN
    "course_type_only":    [],                        // = / IN(与 exclude 互斥,二选一)
    "coord_unit":      ""                              // 从下方注入的学院闭集逐字选,否则 ""
  },

  // program / 单课:语义不变
  "course_code":  "",
  "program_name": "",
  "direction":    "",          // course_to_programs | program_to_courses | permit
  "kb_query":     ""           // 仅 mode=kb 时填英文 KB query
}
```

### 与旧输出的对照
| 旧字段 / 旧正则 | 新位置 |
|---|---|
| `where: "has_exam=false AND level='Postgraduate Coursework'"` | `filters.has_exam=false`, `filters.level="Postgraduate Coursework"` |
| `_exam_intent` 正则 | `filters.has_exam`(+ 否定兜底 double-check) |
| `_group_intent` 正则 | `filters.group_status` |
| `_excluded_types` 正则 | `filters.course_type_exclude` |
| `_semester_intent` 正则 | `filters.semester` |
| `_clean_where` / 列白名单 | 不需要(无自由串);改 `_validate_filters` 按枚举校验 |
| `coord_unit`(Option C) | `filters.coord_unit`,逐字命中枚举才放行(不变) |

## 2. PROMPT 草稿

````text
你是 UQ 课程库查询规划器。把用户问题转成 JSON 查询计划,只输出 JSON,不要解释。

{schema}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【第一步:判 mode(6 选 1)】
你拿到的问题先归到下面 6 类之一。判定顺序:先看有没有课程码/学位名(→ program 或
course_detail),再看是不是学校事务(→ kb),最后才在课程检索三类(filter/semantic/hybrid)里分。

- "course_detail":问题里出现一个课程码(如 CSSE1001),且问的是**这门课本身**
  (介绍/先修/考核/学分/什么时候开)。填 course_code,其它全空。
- "program":问的是**课程 ↔ 专业的关系**,三种 direction:
    · "course_to_programs":课程码 +「是哪些专业的必修/选修」。填 course_code。
    · "program_to_courses":学位名(Bachelor of…/Master of…/学士/硕士)+「要修哪些课/培养方案」。填 program_name。
    · "permit":课程码 + 学位名 +「能不能修/可不可以修/禁不禁修」。填 course_code + program_name。
- "kb":学校事务/政策/日期/服务,**与具体课程或专业无关**。例如开学/census/缴费/退课截止日期、
  重置密码、申请缓考、假期开放时间、停车收费、求助、开在读证明。填 kb_query(英文官方术语)。
  **铁律:只要出现课程码或学位名,就绝不是 kb。**
- 其余都是「在课程库里筛课」,按有没有「模糊主题」分三类:
    · "filter":只有结构化条件(学期/有无考试/hurdle/本研/学分/校区/学院…),**没有**模糊主题。
       filters 填值,semantic_query 留空。
    · "semantic":只有模糊主题/学科(机器学习/网络安全…),**没有**结构化条件。
       semantic_query 填英文,filters 全 null/[]。
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
- 缩写一定要翻成英文全称,**绝不能因为不认识就丢掉**:
  CS=computer science、AI=artificial intelligence、ML=machine learning、
  IT=information technology、EE=electrical engineering。
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
  has_exam 只区分「有没有任何考试」,分不出期中期末。
- 问「期末考试」没有专用列,只能用 has_exam(期末 ≈ 有考试)。

# group_status(小组/团队评估,'has' / 'none' / null)
- 关键词:小组作业/团队作业/group project/groupwork/group assessment/group/team。
- 「有小组作业」-> 'has';「没有小组作业/不含 group」-> 'none'(同样否定优先)。

# level(只有两个合法值,别无第三)
- 本科/本科生/bachelor/undergraduate -> "Undergraduate"。
- 研究生/硕士/master/postgraduate -> "Postgraduate Coursework"。
- **绝不能**写 "Master"/"PG"/"研究生" 这类不存在的值。
- 注意:「Master of Computer Science」是**学位名**(→ program/program_name),不是 level;
  只有「研究生的课/master 的课」这种把 master 当层级用时,才填 level。

# units(学分,数值)
- 「2 学分/2 units」-> 2。没提到 null。

# semester(学期,'S1' / 'S2' / null)
- 第一学期/semester 1/S1 -> 'S1';第二学期/semester 2/S2 -> 'S2'。
- 「两个学期都开/S1 和 S2 都…」这种**跨学期都满足**的全称量词,**semester 仍留 null**
  (交给后端的 both-semesters 合取逻辑处理,你只要别填单个学期即可)。

# course_type_exclude / course_type_only(课程类型,合法值 coursework/placement/research/thesis)
- 「排除/不含/不要某些类型」-> 把类型放进 course_type_exclude(如 ["thesis","research","placement"])。
- 「只要某类型/仅 placement」-> 放进 course_type_only。
- **两个列表二选一**,不要同时填。都没提到就都给 []。

# location / attendance_mode(校区 / 授课模式)—— 红线,务必照抄
- **照搬用户原话的字面值,绝不替换、绝不补全、绝不翻译成别的已知值**。
- 即使用户说的值看起来不在常见枚举里(如 Gatton、Herston、Online、远程),也**原样填**。
  是否在库里由后端判定;你擅自把 Gatton 换成 St Lucia 会把全错的结果当对的返回(严重事故)。
- 没提到校区/模式就给 null。

# coord_unit(开课学院,范围限定,不是主题)
- 用户点名某学院(EECS学院/商学院/某学院的课)时,从下方注入的**真实学院清单**里
  **逐字原样**挑一个最匹配的填进去(不改写/不缩写/不翻译);清单里找不到就留 ""。
- 学院是**范围**不是主题:用户只点名学院 + 结构化条件、而**没有**真正的学科主题时 ->
  semantic_query 留空、mode=filter(返回该学院全部符合条件的课);
  既点名学院**又**有真主题(如「EECS 里跟机器学习相关的」)-> 同时给 semantic_query,mode=hybrid。
- 没点名学院就留 ""。
- 特别注意:计算机/CS/软件 等**学科词不要**当学院填 coord_unit(这类课跨多个学院挂靠),
  应走 semantic_query。只有「显式说了某某学院」才填 coord_unit。

# 组合查询(专业 + 筛选)
- 「Bachelor of X 里没有考试的课」这种**专业范围内再加结构化条件**:
  mode=program、direction=program_to_courses、program_name 填学位名,
  **同时**把结构化条件填进 filters(如 has_exam=false)。后端会在该专业课表内按 filters 取交集。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【例子】(filters 中未写出的键一律保持默认 null / [])

- "没有考试的课" ->
  {{"mode":"filter","semantic_query":"","filters":{{"has_exam":false}},"course_code":"","program_name":"","direction":"","kb_query":""}}
- "没有考试的研究生课" ->
  {{"mode":"filter","filters":{{"has_exam":false,"level":"Postgraduate Coursework"}},...}}
- "Master 没考试的课"(master 当层级用)->
  {{"mode":"filter","filters":{{"has_exam":false,"level":"Postgraduate Coursework"}},...}}
- "哪些课没有期中考试" ->
  {{"mode":"filter","filters":{{"midterm_status":"none"}},...}}
- "有期中考试的课" ->
  {{"mode":"filter","filters":{{"midterm_status":"has"}},...}}
- "没有小组作业的研究生课" ->
  {{"mode":"filter","filters":{{"group_status":"none","level":"Postgraduate Coursework"}},...}}
- "没考试的、不含 placement/thesis/research 的课" ->
  {{"mode":"filter","filters":{{"has_exam":false,"course_type_exclude":["placement","thesis","research"]}},...}}
- "St Lucia 校区 2 学分的本科课" ->
  {{"mode":"filter","filters":{{"location":"St Lucia","units":2,"level":"Undergraduate"}},...}}
- "Gatton 校区的本科课" ->
  {{"mode":"filter","filters":{{"location":"Gatton","level":"Undergraduate"}},...}}
  // Gatton 照抄;后端校验它不在枚举 -> 正确返回空,而不是误命中 St Lucia
- "线上的课" ->
  {{"mode":"filter","filters":{{"attendance_mode":"Online"}},...}}
- "EECS学院下所有没考试的课" ->
  {{"mode":"filter","filters":{{"has_exam":false,"coord_unit":"Elec Engineering & Comp Science School"}},...}}
- "商学院里有期中考试的课" ->
  {{"mode":"filter","filters":{{"midterm_status":"has","coord_unit":"Business School"}},...}}
- "找跟机器学习相关的课" ->
  {{"mode":"semantic","semantic_query":"machine learning","filters":{{}},...}}
- "CS有哪些课没有考试" ->
  {{"mode":"hybrid","semantic_query":"computer science","filters":{{"has_exam":false}},...}}
- "计算机相关、没有 hurdle 的研究生课" ->
  {{"mode":"hybrid","semantic_query":"computer science","filters":{{"has_hurdle":false,"level":"Postgraduate Coursework"}},...}}
- "EECS学院里跟机器学习相关的课" ->
  {{"mode":"hybrid","semantic_query":"machine learning","filters":{{"coord_unit":"Elec Engineering & Comp Science School"}},...}}
- "CSSE1001 是哪些专业的必修" ->
  {{"mode":"program","course_code":"CSSE1001","direction":"course_to_programs","filters":{{}},...}}
- "Bachelor of Computer Science 要修哪些课" ->
  {{"mode":"program","program_name":"Bachelor of Computer Science","direction":"program_to_courses","filters":{{}},...}}
- "Bachelor of Computer Science 里没有考试的课"(组合查询)->
  {{"mode":"program","program_name":"Bachelor of Computer Science","direction":"program_to_courses","filters":{{"has_exam":false}},...}}
- "Master of Data Science 能不能修 CSSE1001" ->
  {{"mode":"program","course_code":"CSSE1001","program_name":"Master of Data Science","direction":"permit","filters":{{}},...}}
- "CSSE1001 这门课讲什么 / 先修是什么" ->
  {{"mode":"course_detail","course_code":"CSSE1001","filters":{{}},...}}
- "census date 是什么时候" ->
  {{"mode":"kb","kb_query":"When is the census date","filters":{{}},...}}
- "怎么申请缓考" ->
  {{"mode":"kb","kb_query":"How to apply for a deferred exam","filters":{{}},...}}

用户问题:{q}
````

> PROMPT 相比旧版:**砍掉**了整段「怎么写合法 SQL」的约束(不碰 title/code、不写分号/SELECT/LIKE、
> 布尔不加引号、列白名单…)—— 因为 LLM 不再写 SQL,这些由 `build_where` 结构性保证。
> **保留并写细**了真正需要 LLM 判断的语言活:否定优先、期中 vs 期末、master 是层级还是学位名、
> 校区照抄红线、学院是范围不是主题。这些是「分类/消歧」的语言任务,正是该交给 LLM 的部分。

## 3. 代码侧骨架(草稿,接口签名为主)

```python
# 三态/枚举槽位的合法值表(确定性,单一真相源;加维度就在这里加一行)
_FILTER_SPEC = {
    "has_exam":       {"kind": "bool"},
    "has_hurdle":     {"kind": "bool"},
    "midterm_status": {"kind": "enum", "vals": {"has", "none"}},
    "group_status":   {"kind": "enum", "vals": {"has", "none"}},
    "level":          {"kind": "enum_cache", "cache": "level"},
    "units":          {"kind": "number"},
    "location":       {"kind": "enum_cache_or_literal", "cache": "location"},
    "attendance_mode":{"kind": "enum_cache_or_literal", "cache": "attendance_mode"},
    "course_type":    {"kind": "enum", "vals": {"coursework","placement","research","thesis"}},
    # semester / coord_unit / course_type_exclude/only 走各自专用路径
}

def _validate_filters(filters: dict, question: str) -> dict:
    """LLM 槽位 -> 校验后的确定性槽位。枚举越界按现有 guard 处理(保留原值/丢弃+日志)。"""
    ...

def build_where(vf: dict) -> tuple[str, list]:
    """校验后的槽位 -> 参数化 (where_sql, params)。只拼枚举值,注入安全是结构性的。"""
    clauses, params = [], []
    if vf.get("has_exam") is not None:
        clauses.append("has_exam = %s"); params.append(vf["has_exam"])
    if vf.get("course_type_exclude"):
        clauses.append("course_type <> ALL(%s)"); params.append(vf["course_type_exclude"])
    # ... 每维度一行
    return (" AND ".join(clauses), params)
```

> **待定 1 — 已定为 A**:`retrieval.filter_search` 等签名改为吃 `filters: dict`,内部调
> `build_where` 产出参数化 `(sql, params)`。`guard_where`/`WHERE_WHITELIST`/`DANGEROUS` 成线上死代码。
> 完整爆炸半径(call sites / 解析器 / 测试 / golden / 6 个 must-fix gaps)见
> `planner-slot-extraction.md` 的「Approach A — 精确爆炸半径」段。
>
> **待定 2**:`_excluded_title_kw`(capstone/project 标题排除)要不要也槽位化,
> 还是保留确定性抽取。按步骤 4 的 eval 结果定。
