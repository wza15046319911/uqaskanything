# Planner 槽位化重构 — 详细计划

分支:`feature/planner-slot-extraction`

## 背景与目标

`planner.py` 现在把自然语言转成查询计划时,LLM 输出的是**一段自由 WHERE 字符串**,
于是必须:
- 用 `_clean_where`(BANNED / TEXT_COLS / LIKE_RE / ALLOWED_WHERE_IDENTS)扫这段不可信字符串;
- 用一堆 `_xxx_intent` 正则把 LLM 漏掉/写错的结构化意图确定性补回来。

后果:**加一种筛选维度要同时改三处**(正则 + PROMPT 例子 + WHERE 列白名单),维护负担重。

目标(纯维护性,不改产品行为):把 LLM 输出从「自由 WHERE 串」改成「类型化槽位对象」,
WHERE 由代码从**校验后的枚举值**拼装。加一种筛选 = schema 加一字段 + 校验一行 + 拼装一行。

非目标:不动 KB 路径;不动 simulator;不改对学生可见的答案语义;不放宽任何红线。

## 前提(已确认)
- 生产 LLM 后端:**DeepSeek 为主**(`llm.use_deepseek()`)。模型可靠,schema 抽取 jitter 可控。
- 痛点:**维护负担**(不是路由准确率)。
- 本地 qwen 7B 仍是降级后端 —— 因此**否定语义 / 枚举越界**两处保留确定性兜底,
  保证弱模型下也不踩红线(student-facing 红线 1/3)。

## 设计

### 1. LLM 输出契约:槽位对象(取代自由 WHERE)

LLM(json_mode)输出见 `planner-slot-schema-draft.md`。要点:
- 不再输出 `where` 字符串;改输出 `filters` 对象,每个字段是**类型化槽位**(bool / 三态枚举 / 数值 / 列表)。
- `coord_unit` 仍从注入的真实学院枚举闭集里**逐字选**(沿用现有 Option C)。
- `mode` / `semantic_query` / `course_code` / `program_name` / `direction` / `kb_query` 字段语义不变。

### 2. 代码侧三件事(全部确定性)

**a. 槽位校验 `_validate_filters(filters, question)`**
- 枚举字段(level / location / attendance_mode / coord_unit / semester / midterm_status /
  group_status / course_type)按 `_ENUM_CACHE` 真实枚举校验。
- 非枚举 location / attendance_mode:沿用现有 guard —— **保留用户原字面值**(使 SQL 命中 0,
  绝不被换成库里有的值)。这是 `_enforce_enum_guard` / `_enforce_attendance_guard` 的语义,
  现在作用在**结构化槽位**上而非 WHERE 串上,逻辑更直。
- 非法 coord_unit:丢弃 + 记日志(沿用 `_validate_coord_unit`,规则 19)。
- 非法三态/bool 值:丢弃该槽(等于该维度不过滤),记日志。

**b. WHERE 拼装 `build_where(filters) -> (sql, params)` 纯函数**(approach **A**,已选定)
- 产出**带 `%s` 占位的 SQL 片段 + 参数列表**,沿用 `_coord_clause` / `_title_exclude_clause`
  既有的 `(sql, params)` 契约。列名来自代码侧闭集(`ALLOWED_WHERE_COLS`),值全进 params。
  **注入安全是结构性的**:没有自由字符串进 SQL,无 SQL 文本可净化。
- `retrieval.filter_search` / `filter_search_both_semesters` / `hybrid_search` / `_fused_search`
  签名改为接受 `filters: dict` 并内部调 `build_where`。`guard_where` / `WHERE_WHITELIST` /
  `DANGEROUS` 对**线上路径变为死代码**(见落地步骤 5 的删除条件)。

**c. 确定性兜底(保留,缩小到高价值项)**
- 否定语义 double-check:`_exam_intent` / `_group_intent` 否定优先,DeepSeek 填了相反值时纠正。
- `_force_program_route` / kb 撤销:纠路由抖动,不变。
- `_LOW_BURDEN` 短路:产品策略(红线 1),不变。
- `_FACULTY_UNITS`:商科/文科→多个学院的一对多查表(数据,非正则 sprawl),保留。
- `_code_level_digits`:首位数字筛年级,保留为确定性抽取(code 不进 WHERE,本就特殊)。

### 3. 删除(被槽位化取代后的死代码)
- `_clean_where`、`BANNED`、`TEXT_COLS`、`LIKE_RE`、`ALLOWED_WHERE_COLS/IDENTS`
- `_strip_semester` / `_strip_semester_any`(语义改由 `semester` 槽 + both_semesters 处理)
- `_exam_intent` / `_group_intent` 作为**主抽取器**降级为否定兜底;
  `_excluded_types` / `_excluded_title_kw` / `_semester_intent` 作为主抽取器删除
  (其值改由 LLM 填槽,exclude_title 仍走参数化 NOT ILIKE)。

> 注:`_excluded_title_kw`(capstone/project/review… 标题排除)若 LLM 填槽不稳,
> 可保留为确定性抽取。落地时按 eval 结果定 —— 见验证步骤。

## Approach A — 精确爆炸半径(已由 4 路并行 reader + 对抗校验绘制)

下列行号以重构前的当前代码为准。

### retrieval.py
- **新增** `build_where(filters) -> (sql, params)`(见上 §2b)。
- `filter_search`(L164-185):`where:str` → `filters:dict`;删 `guard_where`(L171);
  `full_where` 字符串拼接(L175)改为 `build_where` 的片段 + 把 `where_params + coord_params
  + title_params` 按序穿进 `conn.execute`(L179)。
- `filter_search_both_semesters`(L188-219):`base` 在外层 WHERE 与子查询 HAVING **出现两次**,
  今天传 `base_params + base_params`(L211);现在 `where_params` 进 `base_params`,
  整组 `(where_params+coord_params+title_params)*2`。`semester IN ('S1','S2')` 字面量(L200)留代码。
- `hybrid_search`(L250-261)→ `_fused_search`(L264+):删 `guard_where`(L270);
  `where_params` 须插到 `vec_sql`(L284)与 `kw_sql`(L294)**两处** `%s` 的精确文本位置
  (见 Risk 3,最易错的一行)。
- `guard_where`(L82-102)/ `WHERE_WHITELIST`(L75)/ `DANGEROUS`(L79):线上路径死代码。
- 无改动:`semantic_search` / `keyword_search` / `course_detail` / `kb_search` /
  `_coord_clause` / `_title_exclude_clause`(本就参数化)。
- **GAP3(校验补)**:`retrieval.py` 模块 docstring(L10/12/15)写了 `guard_where`/`filter_search`/
  `hybrid_search` 旧签名 —— 须同步更新(项目中文 docstring 约定,规则 18)。

### planner.py
- `plan`(L636+):改 PROMPT(列表 L210-211、输出结构 L231、例子 L234-258)输出 `filters` 对象;
  改解析(L682-683)、输出 dict 的 `"where"` 键 → `"filters"`(L691);
  后处理流水线 L709-719(`_clean_where`→`_enforce_enum_guard`→`_enforce_attendance_guard`
  →`_strip_semester_any`)全部改作用于 dict。
- 低负担快路径(L660-666):硬编码 `base` SQL 串 → 硬编码 filters dict。
- `_clean_where`(L310-326)删除 → `_validate_filters`;`BANNED`/`LIKE_RE`/`TEXT_COLS`/
  `ALLOWED_WHERE_IDENTS`(L161-165)废弃。
- `_enforce_enum_guard`(L347)/`_enforce_attendance_guard`(L396)/`_enforce_level_hint`(L598)/
  `_force_where_clause`(L590):regex 检测+`re.sub`/拼接 → dict 赋值。三个 level 调用点 L633/664/818 同改。
- `_program_filter_where`(L615-633):`conds.append(f"...")` → `filters[key]=val`;
  `course_type NOT IN(...)`(L632)→ `filters["course_type_exclude"]=types`。
- `_strip_semester_any`(L499)/`_strip_semester`(L580):→ `filters.pop("semester", None)`。
  `_strip_semester` 无调用点(删前 grep 确认)。

### qa.py
- 调用点 L350-355 / L373-374 / L412 → 传 `p["filters"]`(随 retrieval 签名)。
- 真值门 L404/L407:`not p.get("where")` → `not p.get("filters")`(空 dict 即 falsy)。
- meta 序列化:L360-361 / L375 / **L419-420** 需 `filters → text` 序列化器。
- 解析消费者(见 §「hard cases」):`_empty_note`(L289-301)、`_status_unknown_note`(L550-580)、
  KB 兜底门 `_COURSE_DIM`(L486,def L39)、`_status_note`(L505)。

### sim_advise.py
- L113 `_program_filter_where(goal)` 现返回 dict;L118/L151 `filter_search(conn, dict)`;
  L116/L146 真值判断对空 dict 成立;L119-121 `except ValueError` 降级支线见 Risk 5。

### 测试 / golden / harness
- `data/eval/routing.jsonl`:`where_has`(~30 条)/ `where_equals`(L35/86/88/89)golden 字段重写为断言
  结构化 filter 键/值。**GAP5(校验补,最关键)**:`where_has:["gatton"]`(L6/L37)断言的是
  **值**子串(`gatton`∈`location='Gatton'`),不是列名 —— 重写必须断言 `filters["location"]=="Gatton"`,
  机械地映成「断言键存在」会变成 rule-16 弱测试(假过)。
- `app/pipelines/route_eval.py`:`_route_of`(读 `p.get("where")`)+ `_check`(子串匹配)改读 dict。
- `tests/test_planner.py`:`test_program_filter_where_rebuilds_structured_conditions`、4 个
  `_enforce_level_hint` 测试、`test_low_burden_short_circuits_before_llm`、`test_strip_semester_any`
  —— 全断言 SQL 串,改断言 dict。
- **GAP2(校验补)**:`tests/eval.py` 有**两处**:L163(`guard_where`)+ **L236-237**(`_enforce_level_hint`
  断言 SQL 串)。后者同 test_planner 一起改。
- 无改动:`data/eval/answers.jsonl`(`courses_satisfy` 断言返回行,留作端到端回归网)、`../eval` deepeval(走 HTTP 解耦)。

### GAP1(校验补)—— 死的重复模块 `query.py`
`app/services/query.py` 自带**另一份** `guard_where`(L92)+ `where` 版 PROMPT(L41-47),
但**全仓零 import**(已 grep 确认,是被 planner 取代的「阶段四」原型)。不阻塞迁移,但步骤 5 删
`guard_where` 时须**显式声明**这份孤儿副本是留还是删,不能默认忽略(规则 14:两份同名定义)。

## Hard cases —— 把「从 SQL 串读回语义」改成「从 dict 读」(§见校验已确认可行)

不变量:**planner 的 `_enforce_*` 写入 dict 的 schema,必须与 qa 这些解析器读的 schema 一致**。

1. **`_empty_note`**(qa L289):`_ATTEND_VAL_RE`/`_LOC_VAL_RE` 捕获 `attendance_mode='...'`/
   `location='...'` 的字面值并查 `_ENUM_CACHE` → 改为直接读 `filters.get("attendance_mode"/"location")`,
   枚举成员判定**逐字不变**(这是 Gatton/Online 越界的确定性学生提示,绝不能丢)。删两个正则。
2. **`_status_unknown_note`**(qa L550,最高风险):`none_re.search`+`none_re.sub('unknown')`+重跑
   filter_search → 改为 `unk = dict(filters); unk[col] = "unknown"` 再喂 `filter_search`。
   **GAP6(校验补)**:翻转后的 dict 经 `build_where` round-trip(`unknown` 是合法枚举值),
   这正是替代旧 `guard_where` 再校验的安全保证 —— 计划须写明「翻转 dict 走 build_where,不是裸 SQL」。
3. **`_COURSE_DIM` 门**(qa L486,def L39):正则只匹配 6 维(`level|units|has_exam|has_hurdle|
   location|attendance_mode`),**漏** `midterm_status`/`group_status`/`course_type`。改 dict 键集判断;
   **先与原正则行为完全一致(只这 6 维)**,扩维另行提案(规则 10/18:不顺手改行为)。
   误迁会翻转「日期问题→KB」的确定性路由。
4. **`_validate_filters`** 取代 `_clean_where`:逐键校验(未知键丢弃+surface,规则 19;bool 须 bool、
   枚举须在 `_ENUM_CACHE`、units 须数值、course_type_* 须 `{coursework,placement,research,thesis}` 子集)。
   **关键区分**(Risk 4):非枚举 `location`/`attendance_mode` 是 planner 故意强制(Gatton/Online 制造空集),
   **不能当垃圾丢弃** —— 要让它原样到 `_empty_note` 触发提示。区分「故意非枚举」与「真垃圾值」。

## 提议签名(草稿见 planner-slot-schema-draft.md §3,以下为 retrieval 侧补充)
```python
def build_where(filters: dict) -> tuple[str, list]: ...          # ("", []) on empty
def filter_search(conn, filters: dict, order_by="code", coord_units=None, exclude_title=None) -> list[dict]
def filter_search_both_semesters(conn, filters: dict|None=None, coord_units=None, exclude_title=None) -> list[dict]
def hybrid_search(conn, filters: dict|None, semantic_en: str, k=8, coord_units=None) -> list[dict]
```
**穿参铁律**:params 列表顺序必须跟 SQL 里 `%s` 的文本先后一致。`_fused_search` 里 `vec` 的 `%s`
在 filter 片段之前,故顺序是 `(vec, *where_params, *coord_params, vec, pool)`。错序 psycopg 不报错、
静默绑错值 —— confidently wrong,不是崩溃。

## 落地顺序(approach A;每步可跑、可回滚;规则 17)

1. ✅ **立基线**(已完成,见附录)。
2. **加 `build_where` + `_validate_filters`,不改任何调用方**。用现有 `guard_where` 自测用例
   (retrieval L453-482)对照单测 `build_where`:同样的逻辑 WHERE,现在是 `(sql with %s, params)`。
   `pytest` 绿,未接线。**Checkpoint**。
3. **切 retrieval 内部吃 dict**(filter_search / _fused_search / both_semesters),planner 暂不动。
   过渡 checkpoint 间可临时双吃(dict 新 / 旧路),但**不长期混用**(规则 14),步骤 5 删。跑集成测试。
4. **planner 翻成发 `filters`**:PROMPT+解析、`_program_filter_where`、`_enforce_*`、低负担路径改 dict 操作;
   `_clean_where`→`_validate_filters`;`_strip_semester_any`→`filters.pop`。同步 qa.py 调用点 +
   sim_advise。再迁 qa 解析器(`_empty_note` / `_status_unknown_note` / `_COURSE_DIM`)+ meta 序列化器。
   **Checkpoint**:`answer_eval` 的 `courses_satisfy` 是回归网;补单测
   `_empty_note({"location":"Gatton"})` 必触发校区提示。
5. **改 golden + harness,删死代码**:重写 `routing.jsonl` 的 where_has/where_equals(注意 GAP5 值断言)+
   `route_eval._route_of`/`_check` + `test_planner.py` 串断言 + `tests/eval.py` L163/L236。
   确认无线上调用后删 `guard_where`/`WHERE_WHITELIST`/`DANGEROUS`/`_clean_where`/`_strip_semester`
   + 其测试;显式处理 `query.py` 孤儿副本(GAP1)。终跑 `pytest` + `route_eval` + `answer_eval`。

## Risks specific to A(红线面)
1. **空 filters 全表扫**:旧 `guard_where("")` 会 raise(L89),故 filter_search 不可能空 WHERE。
   `build_where({})` 返回 `("", [])`,若 filter_search 退化成 `WHERE TRUE` 会静默返回全表(踩红线 5)。
   **决策(已定):filter_search 空 filters 时 raise ValueError**,与 `guard_where` 旧位置一致。理由:
   - 空 where 现有**两层**防护——planner 兜底 3(L857,filter/hybrid 无条件无主题时 raise→qa 接成 empty)
     + filter_search 边界 raise。Option 1 两层都保住,对 100% 基线改动最小。
   - 把 raise 留在 filter_search 让 qa 的 `try/except ValueError`(L356/L376/L413)**仍能触发,不成死代码**
     —— 同时化解 Risk 5;反之 qa 提前路由 empty 会使这些 except 永不触发,须删,反移除纵深防御。
   - `build_where` 仍是纯函数(空→`("", [])`),供 both_semesters 合法地空 filters + 补 `semester IN('S1','S2')`;
     raise 只在 filter_search。both_semesters 故意容忍空(永不真全表扫),不 raise。
   - 兜底 3 同步改为判 `not out["filters"]`(空 dict)而非 `not out["where"]`。
2. **both_semesters 双份 params**:`base` 出现两次,`where_params` 必须进双份组;只 copy 旧的 coord/title
   双份会 `%s` 数不匹配 → IndexError / 绑错,污染「两学期都」这个 enrolment 事实。
3. **hybrid 穿参顺序**(见上铁律)。补集成断言:`hybrid_search({"level":"Postgraduate Coursework"},
   "data science")` 只返回研究生行。
4. **`_validate_filters` 静默放过**:非法值不能静默 coerce(规则 19);且非枚举 location/attendance 是
   故意的,不能丢(Risk 4 / hard case 4)。
5. **死降级支线**:`guard_where` 不再 raise 后,qa L356/L376/**L413**(GAP 校验补,原 synthesis 漏)+
   sim_advise L119 的 `except ValueError` 可能永不触发。须显式决定:让 `build_where` 对畸形 dict raise
   以保留支线,或删支线(不留永不触发的 try/except 误导读者)。

## 迭代预算(全局规则 13)
- 每步最多 3 轮调试;eval 跌且 2 轮内修不回 → 停下回滚到上一步,带现状汇报。
- route_eval / answer_eval 任一**低于基线**即视为失败,不得「差不多」放行。

## 校验补的 6 个 must-fix(对抗 reviewer 给出,已并入上文对应处)
1. GAP1 `query.py` 孤儿 `guard_where` 显式处理(落地步骤 5)。
2. GAP2 `tests/eval.py` L236-237 + GAP5 `routing.jsonl` L6/L37 值断言(测试/golden 段)。
3. GAP4 `qa.py:419 program_facts["filter"]=p["where"]` 是**客户端可见**字段(经 meta 到前端),非纯 debug,
   需 `filters→text` 序列化器,并确认下游无人解析它。
4. GAP(Risk5)`qa.py:413` 并入死降级支线决策。
5. GAP3 `retrieval.py` 模块 docstring 签名同步(规则 18)。
6. GAP6 `_status_unknown_note` 翻转 dict 走 `build_where` round-trip(hard case 2)。

## 附录:基线分数(步骤 1,backend=deepseek,DB 3049 门课)
- route_eval:**110/110 (100%)** — course_detail 12/12、filter 28/28、hybrid 11/11、kb 22/22、program 26/26、semantic 11/11
- answer_eval:**40/40 (100%)**(含虚构实体拒答 5 题正确拒答)
- pytest:**146 passed**

> 重构后这三项任一**低于基线**即视为失败(规则 11/16、红线 6)。LLM 后端非确定性,
> 允许 ±1 题抖动复跑确认;系统性下跌不得放行。
> 日志存档:/tmp/route_eval_baseline.txt、/tmp/answer_eval_baseline.txt

## 执行记录(完成,零回归)

最终分数(backend=deepseek,DB 3049 门课):
- **pytest 164 passed**(原 146 + 新 `test_planner_slots.py` 18;删 `test_strip_semester_any`、
  `test_where_guard.py` 重写为「注入安全结构性」回归,净持平)。
- **route_eval 110/110 (100%)** — 各 mode 分组与基线逐组一致。
- **answer_eval 40/40 (100%)** — 含 5 题虚构实体正确拒答。

落地与计划的两处**有意偏离**(均有据):
1. **步骤 3、4 合并为一次原子契约切换**:`_program_filter_where` 同时被 qa 与 sim_advise 消费、
   `filter_search` 被三方调用,契约强耦合,无法在不加「双吃」垫片下分两步且每步 pytest 绿。
   全局 CLAUDE.md 规则 3 禁止未经询问加兼容代码——双吃恰是兼容垫片。故合并,同步改全部调用方 +
   被改函数的单测,checkpoint 处 pytest 仍绿(守规则 17)。
2. **routing.jsonl golden 零改动**:`where_has`/`where_equals` 的列名/值断言(含 GAP5 的 gatton 值)
   全部能被「filters 经 `retrieval.describe_where` 渲染回 where-like 串」原样命中——比机械重写成
   「断言键存在」更保真(守 GAP5 + 规则 16,不造弱测试)。`describe_where` 作为 `build_where` 的可读
   对偶(单一真相源)放 retrieval,qa 的 meta/GAP4 与 route_eval 共用。

未做(交用户决定,规则 1 禁 rm + committed 文件非我创建):
- **GAP1 `app/services/query.py` 孤儿**:零导入、被 planner 取代的「阶段四」原型,仍带与槽位设计
  矛盾的 `guard_where` 副本 + where-PROMPT(规则 14)。建议用户 `git rm app/services/query.py`。

已删死代码(用 Edit,正常重构):
- retrieval:`guard_where` + `WHERE_WHITELIST`/`DANGEROUS`/`ALLOWED_COLS`/`_COL/_CMP/_LIT/_IN_LIST/_COND`
  + `__main__` guard 自测块(换成 build_where 演示)。
- planner:`_clean_where` + `BANNED`/`LIKE_RE`/`TEXT_COLS`/`ALLOWED_WHERE_COLS`/`ALLOWED_WHERE_IDENTS`
  + `_strip_semester_any`/`_strip_semester`/`_force_where_clause`/`_semester_intent`(后四者本就/已成死代码)。
- `_excluded_title_kw` 经 eval 验证稳定,**保留**为确定性抽取(原待定 2 落定:不槽位化)。

维护性达成:加一种结构化筛选维度,从「改正则 + 改 PROMPT 例子 + 改 WHERE 列白名单」三处手写正则,
变为「PROMPT 加一槽 + `_FILTER_SPEC` 加一行 + `_WHERE_BUILDERS` 加一行」三处声明式表项,无正则、
无自由 SQL 净化。

## 追加:code_level 并入统一槽位系统(用户后续要求,选项 A)

把唯一还在新系统外的筛选维度 `code_level`(按 code 首位数字筛年级)迁入 filters 槽位:
- **抽取仍确定性**(`_code_level_digits` 正则,不交 LLM——code 首位是 rule-12 的结构化事实)。
- 不再 qa 层 Python 后过滤;改由 `build_where` 出 `substring(code from '[0-9]') = ANY(%s)` 走 SQL
  (POSIX 取首个数字字符,逐字等价旧 `_first_digit`;值进 params,注入安全)。
- 登记进 `_FILTER_SPEC`(digit_list)/ `build_where` / `describe_where` 三处;`_validate_filters` 加 digit_list 校验。
- plan() 在确定性抽取点注入 `out["filters"]["code_level"]`;program 组合分支重置 filters 后再注入一次
  (不改 `_program_filter_where`,故 sim_advise / `_force_program_route` 行为不变)。
- 删 qa 的后过滤 + `_first_digit` + `_status_*` 的 code_levels 参数(翻转 dict 经 filter_search 自带同范围统计)。
- **有意行为变化**:含 code_level + 主题的问题由 semantic 升 hybrid(结构化条件入池更准);
  纯 code_level 问题从「空」变为可检索。route_eval 110/110、answer_eval 40/40、pytest 167 全过,零回归。

## 追加:校验层换成 pydantic v2(用户后续要求,对齐行业标准)

`_validate_filters` 的内部从手写 `_FILTER_SPEC` + if/elif 链换成 pydantic v2 `_FiltersModel`。
先做了一次性等价实验(49 条契约用例,手写 vs pydantic 行为 0 分歧;行数手写逻辑68 / pydantic66,基本平手),
结论是横向移动——遂按用户决定迁移。**只换 `_validate_filters` 内部,保持 dict 进 / dict 出边界**:
`plan()` / `build_where` / `describe_where` / `_enforce_*` / qa 全部消费纯 dict,一行不动(不做全程传 model 的大改)。

要点(逐字段 `mode="before"` validator 复刻确定性语义,不吃 pydantic 默认 coerce):
- `has_exam`/`has_hurdle`:`isinstance(v,bool)` 才收,`"false"`/`1` 丢(裸 bool 字段会被 lax coerce,违反规则19)。
- `units`:声明 `int|float|None`(**不是 `float`**,否则 `2` 被吞成 `2.0` 与旧行为分歧),走 `_as_number` 不硬转。
- `level`:validator 内读 `_ENUM_CACHE`(活体 DB 枚举,静态 schema 表达不了)。
- `location`/`attendance_mode`:literal 照搬原值不校验枚举(Gatton/Online 红线)。
- 规则19 日志不丢:未知键由 `@model_validator(mode="before")` 比对 `model_fields` 记日志后丢;
  单字段非法由 validator 用 `info.field_name` 记日志 + `return None`(drop-per-key 不抛)。
- 删 `_FILTER_SPEC`(纯内部,测试零引用);`_validate_filters` 公共签名/测试断言全部不变。
- pydantic 2.13.4 已在 requirements(FastAPI 自带),零新增依赖。

验证:`test_planner_slots`/`test_where_guard`/`eval.py#4` 断言不改仍过;pytest 167 / route 110/110 / answer 40/40,零回归。
