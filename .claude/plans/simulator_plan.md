# 选课模拟器实现计划(以 Bachelor of Computer Science / 2559 为例)

> 目标:把已有的确定性引擎 `simulator.py` 做成**交互式选课模拟器**——网页里点课程 / 选 major,各规则进度条和「还能选什么」实时变化;后续接入**先修 gating**、**按学期排课**、**AI 选课建议**。
> 范围由用户拍板:① 交互式 Web UI + API(核心)② 先修课作为独立后置阶段 ③ 进度追踪 + 按学期排课 ④ AI 建议层作为可选后置阶段。
>
> **交付策略(用户定):先做 MVP,后续再演进成正式 web application(React.js + FastAPI)。**
> - **MVP**(本计划阶段一 + 阶段二):后端复用现有 `server.py`(已是 FastAPI),前端用 **vanilla JS 单页**(复刻 `index.html` 视觉)最快跑通选课交互;状态用浏览器 **localStorage** 存,不建表。
> - **演进**(MVP 验证后):前端从 vanilla 迁到 **React.js**(组件化规则段 / 进度条 / 看板),后端 FastAPI 不变(MVP 的 `/api/sim/*` 契约即 React 版的 API),localStorage 不够时再加 `student_plan` 表 / 登录。
> - 阶段三/四/五(排课 / 先修 / AI 建议)是 MVP 之后的增强,与前端是 vanilla 还是 React 解耦——它们只依赖 `/api/sim/*` 契约。

---

## 0. 现状(不要重复造)

- **引擎已存在**:`simulator.py` 的 `PlanSimulator(conn, program_id)` 已实现 `select` / `deselect` / `choose_plan` / `status()` / `available()`,units_max 封顶、equivalence 去重、major 择一、自引用排除都做好了,CLI 自测断言全过。**它是纯确定性代码,不调 LLM。**
- **但它只是 CLI 自测**(`if __name__=='__main__'`),没接任何 API、没接前端、没进 `qa.py`。现有 `server.py`(8077,`/api/ask`)和 `web/index.html` 只服务问答,**完全没碰模拟器**。
- 本计划 = 「**把引擎暴露出来 + 三个增强阶段**」,引擎核心逻辑基本不改(只补只读的分组/锁态查询方法)。

BCompSc(`program_id = "2559"`,48 学分)顶层规则(已实测):

| ref | 标题 | select_type | 必需 | 上限 | 说明 |
|----|------|------------|-----|-----|------|
| A | BCompSc Core Courses | all | 24 | — | 核心必修 |
| B | BCompSc Major Option | select | 16 | — | **4 选 1 major**:ARTINC2559 AI / CYBERC2559 Cyber Security / DATASC2559 Data Science / PROTHC2559 Programming Theory(各 16 学分)→ 引擎的「择一」语义对它正确 |
| C.1 | CS Introductory Electives | select | 2 | 16 | |
| C.2 | CS Advanced Electives | select | 4 | 22 | |
| D | Breadth Electives | select | 0 | 16 | units_min=0,初始即 done |
| F | General Electives | select | 0 | 16 | 同上 |

---

## 1. 数据天花板(必须正视,贯穿所有阶段)

这些是**事实约束**,不是 bug,计划里每个阶段都按它降级处理,绝不假装覆盖完整:

| 约束 | 事实 | 对实现的影响 |
|------|------|------------|
| 课程范围 | `courses` 只有 **S1 / St Lucia / In Person**,1508 门 | 2559 初始 `available()` 49 门里 **21 门(S2-only,如 COMP2200/COMP3506/STAT1301)在 courses 没有行、没有 embedding** |
| 课程码非唯一 | 一门课多 offering(如 PHTY4402 有 3 行) | enrichment 必须 `SELECT DISTINCT ON (code)` 防行翻倍 |
| 无先修数据 | `courses` 只有 `incompatible`,**没有 `prerequisite` 字段**;`scraper.py` 只解析 incompatible | 先修 gating 必须**重爬一遍课程页**;2559 覆盖上限 **44/77 码**(33 个 S2-only 码永远爬不到先修) |
| 无 S2 开课数据 | S2 只有 `s2_course_codes.txt`(2074 码)+ link 清单,**0 个 profile / 开课表** | 「按学期排课」的 offering 约束**默认关闭**,S2 槽位一律标「未核实」 |
| AI 建议可达池 | 49 个 available 里只有 **28 门**同时有 courses 行 + embedding | AI 建议**最多只能覆盖 28/49**,必须向用户**显式披露**这 21 门被排除 |

> 一句话:**S1-only 数据集是所有上层功能的天花板。** 交互模拟器(进度/可选)不受影响(它只读 rules 树);受影响的是排课验证、先修覆盖、AI 建议召回——计划里都做了优雅降级 + 显式报告。

---

## 2. 阶段划分与依赖图

```
阶段一 API 层(root,无上游依赖,programs/courses/引擎都现成)
      │  冻结 state 契约 {program_id, selected[], chosen_plans[]}
      ├──────────────┬───────────────────────────┐
      ▼              ▼                            ▼
阶段二 Web UI     阶段三a 排课 scheduler       阶段四 AI 建议
(依赖一的契约)    (依赖一;与三b 并行)        (依赖一 + Ollama)
                      ▲
阶段三b 先修 prerequisite(独立,需重爬;不阻塞模拟器上线)
                      │ 产出 prereq_map
                      ▼
                 阶段五 排课收紧(三b 完成后,真拓扑排序)
```

**实施顺序**:一 → 二 →(三a ∥ 三b)→ 四 → 五。
三a / 三b / 四 互不阻塞;先修(三b)尾巴最长(重爬 1508 页 + 解析正确性 + 迁移)且收益覆盖最低,**不要排在核心 UI 之前**。

---

## 阶段一:API 层(把引擎暴露成 HTTP)

**核心决策:无状态(stateless)。** 客户端持有完整 state `{program_id, selected[], chosen_plans[]}`,每次操作把全量 state POST 上来;服务端每请求新建一个只读连接、重建 `PlanSimulator`、重放 `select()/choose_plan()`、返回 `status()` + `available()`。

理由:① 引擎的 select 是幂等集合插入、可交换,重放结果确定;`choose_plan` 客户端已发「已解析的最终集合」,重放无顺序风险。② 无 session 存储(无 Redis / 无内存增长 / 刷新不掉),契合「最小代码」。③ 复刻现有 `/api/ask` 的 `with psycopg.connect(DSN) as conn: conn.read_only=True` 模式,零并发耦合。

### 1.1 三个端点

```
GET  /api/sim/programs              -> [{program_id, title, total_units}, ...]  (按 title 排序)
GET  /api/sim/program/{program_id}  -> {program_id, title, total_units, rules:[...status() 空态...]}
POST /api/sim/state  {program_id, selected[], chosen_plans[]}
     -> {program_id, title, total_units, selected, chosen_plans,
         rules:[...status()...],                 # 每条规则进度,引擎原样透出
         available_by_rule:{ref:[slot,...]},      # 按规则分组的可选 slot(见 1.2)
         #   slot = {"kind":"course","code":X} | {"kind":"equiv","options":[X,Y]}  ← 二选一进 v1
         courses:{code:{code,title,units,level,semester,has_exam}}}  # 卡片元数据(见 1.3)
```

### 1.2 返回契约:采用「按规则分组」形态(解决审查发现的契约冲突)

> 审查抓到:扁平 `available:[{code,...}]` 无法让前端按规则分区渲染;前端需要 `available_by_rule`。**采用分组形态**,API 侧据此补 helper。

在 `simulator.py` **新增一个只读方法**(不改 `available()` 签名,避免破坏现有 CLI 自测):

```python
def available_by_rule(self) -> dict[str, list[dict]]:
    # 复用 available() 的口径:跳过已 done 的规则、扣掉已选码、去重、稳定顺序;
    # 按顶层规则 ref 分组;并把 equivalence 项聚成一个 slot(二选一,v1 要分组卡),
    # 普通 course 各自成 slot。
    # slot = {"kind":"course","code":X} | {"kind":"equiv","options":[X,Y,...]}
    # (equiv 的 options 只列「未满足」的备选:某项已选其一就整组不再出现,口径同 available())
```

**陷阱(审查标注,必须照做)**:不能简单地 `for rule: _collect_codes(rule, True)`——规则 D/F 的 `_collect_codes` 会返回 22 个码,但它们因为 D/F 已 done 而**不在** `available()` 里。`available_by_rule()` 必须**跳过 done 规则 + 扣已选码**,使「各 slot 码拍平去重 == `available()`」恒成立(这是验收断言)。引擎里 equivalence 项本就是 `{kind:"equivalence", options:[{code,...}]}`,聚 slot 直接读这个结构即可,无需新数据。

### 1.3 enrichment:LEFT-join + DISTINCT ON(绝不丢码)

```python
def _hydrate(conn, codes: list[str]) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT DISTINCT ON (code) code, title, units, level, semester, has_exam "
        "FROM courses WHERE code = ANY(%s) ORDER BY code", (codes,)).fetchall()
    # 缺行的码(21 个 S2-only)返回 title=None 等,前端用 index.html 已有的
    # 「(本学期无开课信息)」占位兜底。绝不 INNER join、绝不丢码。
```

### 1.4 错误处理(不吞错)

- `PlanSimulator.__init__` 对未知 program_id 抛 `ValueError` → **404**。
- `choose_plan` 对未知 / 自引用 plan 码抛 `ValueError` → **400**,把异常文本返回前端(复刻 `server.py:42-43`)。其它异常 → 500。
- **决策**:`chosen_plans` 信任客户端已解析的集合,重放即可(2559 是单 major,同组重复不会发生);多 major 程序需另行处理,文档标注。

### 1.5 文件改动 / 验收

- 改 `server.py`:加 `import simulator`、`SimState(BaseModel)`、`_hydrate` helper、3 个路由(~40 行,内联,匹配现有扁平模块风格)。
- 加 `simulator.py`:`available_by_rule()` 一个只读方法。**不改** `available()`。
- **验收**:`GET /api/sim/programs` 返 335 行;`GET /api/sim/program/2559` 返 total_units=48 + 6 条规则(B 带 4 个 plan_options);`POST /api/sim/state` selected=[CSSE1001,CSSE2002] → 规则 A `units_counted=4.0`、available 49→47;空态返 49 码,**恰 28 有 title、21 为 null**(若 INNER join 把 21 丢掉=失败);未知 plan 码返 4xx 非 500。

---

## 阶段二:Web UI(交互式选课页)

**决策:新建 `web/simulator.html`,新路由 `GET /sim`,复刻 `index.html` 视觉**(`:root` 调色板、`.course`/`.grp`/`.oror` 卡片、字体、骨架屏全部 copy),**不动 `index.html`**(只在批准后加一个 `/sim` 链接)。理由:index.html 的 DOM/脚本是「查询」形态(单 query string),硬塞一个状态机会变成一个文件两套不相关状态模型。

> 命名注意(审查):index.html 已有 CSS 类 `.sim`;新页容器**不要叫 `#sim`**,用 `#simroot` 之类避免样式串味。

### 2.1 布局(ASCII 草图)

```
.wrap(760px,复用)
  header:  [badge 模拟选课·48 学分]  Bachelor of Computer Science
           program 选择器 <select>(v1 默认且仅 2559)
  总进度条: 已选 14 / 48 学分  ███████░░░░░░  29%   [重置]

  ── 规则段 A(.rulesec,新)──────────────────────
     A · BCompSc Core Courses          [必修]
     4 / 24 学分  ████░░░░░░░░░░░░ (units_counted/units_required)
     .grid 卡片:[CSSE1001]✓选中(amber 环) [CSSE2002] …(点击切换)
  ── 规则段 B — Major(特殊:radio 择一)───────────
     B · BCompSc Major Option   选一个方向   0/16
     ( ) ARTINC2559 AI 16u   (•) CYBERC2559 Cyber 16u(mint 环=已选)
     ▸ 选定 major 后,其课程作为卡片出现在下方
  ── 规则段 C.2 ────────────────────────────────
     C.2 · Advanced  12 / 4 学分  ██████ [已超上限 over_max=amber 警告]
  footer(复用)— 数据为 S1 / St Lucia / In Person
```

### 2.2 客户端状态流(纯视图,不算任何进度——所有数字来自 API)

```
state = loadLS() ?? {program_id:"2559", selected:[], chosen_plans:[]}   // localStorage 恢复
onCourseClick(code):  toggle code in selected;        saveLS(); postState()
onMajorPick(plan):    state.chosen_plans = [plan];     saveLS(); postState()   // 择一=替换不 push
onReset():            selected=[]; chosen_plans=[];    saveLS(); postState()
postState():  POST /api/sim/state(全量 state) -> renderSim(resp)     // 唯一渲染函数
saveLS/loadLS: localStorage 存 {program_id,selected,chosen_plans}(决策①:本地存够,不写库)
```

进度条宽度 = `min(units_counted/units_required, 100%)`;`over_max=true` 时 fill 转 amber + 「已达/超上限」pill,但 `units_done` 原值照显。D/F(done、units_min=0、wildcard 无可枚举课)渲染成「已满足」段,**不要**渲染成坏掉的空段。

### 2.3 v1 取舍 / 验收

- **v1 只支持 2559**(其它 334 个 program 的 rules 树未验证,可能有 select-many major / 更深嵌套)。
- **二选一(equivalence)分组卡进 v1**(决策②):MATH1061|MATH1081 等 5 组渲染成「二选一」卡,**复用 index.html 已有的 `.course.grp` / `.oror` / `groupCard` 模式**——数据来自 §1.2 `available_by_rule` 的 `{kind:"equiv",options:[...]}` slot,前端按 slot.kind 分发渲染(course → 普通卡,equiv → 二选一卡)。点其中任一选项即 `select(code)`,选后整组从可选收敛。
- **状态持久化进 v1**(决策①):localStorage 存 `{program_id,selected,chosen_plans}`,刷新/重开自动恢复;不写库、不登录。
- **验收**:初始渲染 6 段、数字与 `status()` 一致;选 CSSE1001+CSSE2002 → A 条 4/24、两卡选中态、从可选移除(49→47);选 ARTINC2559 radio → 展开其课(47→53),再选别的 major **替换**(绝不两个高亮);**MATH1061|MATH1081 渲染成一张二选一卡**,选 MATH1061 后整组消失;C.2 超 22u → amber 超额、条封顶 100%;**各 slot 码拍平去重 == available()**(尤其规则 D 显示 0 个可选,不是 `_collect_codes` 的 22 个);刷新页面后 selected/major 从 localStorage 恢复。

---

## 阶段三a:按学期排课(scheduler.py,纯确定性,无 LLM)

**决策:贪心拓扑 + 装箱(greedy topological bin-pack),不用 CP-SAT/ILP 求解器。** 理由:约束很弱(48u/6 学期、先修边今天还没有、offering 只有 S1),~60 行确定性代码可调试,契合「最小代码」;数据到位后同一函数自动收紧,无需重写。

```python
def schedule(selected, prereq_map=None, offering_map=None, units_map=None,
             units_cap=8.0, n_semesters=6, semester_labels=None) -> dict:
    # 返回 {"semesters":[{"label":"Y1 S1","courses":[{code,units,verified_offering}], "units":6.0}],
    #       "unplaced":[{code, reason}],        # 绝不静默丢:placed + unplaced == len(selected)
    #       "warnings":["offering 未知 N 门,未核实放置", "先修边指向计划外课程 X 已丢弃"]}
```

算法:① 在 `selected` 内建先修 DAG(指向计划外课的边记入 warnings,不静默忽略);检测环 → 入 `unplaced(reason="prereq_cycle")` 不崩。② Kahn 拓扑排序,稳定 tie-break `(units desc, code asc)`。③ 按拓扑序装箱:课 c 最早合法学期 = 1 + max(已放先修的学期);从该学期起找第一个满足 `units + cap` 且 offering 允许的槽;放不下 → `unplaced(reason="no_fitting_semester")`,**绝不超 cap**。④ 每个放置标 `verified_offering`,未核实数量汇总进 warnings。

**数据降级(关键)**:`offering_map` **默认 None**(全部当未知 → 允许任意学期、标未核实)。当前 `courses.semester` 只有 'S1' 一个值,若用它建 map 会**错误地禁止所有 S2 放置**——所以 S2 profile 爬到之前,offering 约束不开,放置一律「未核实」。

**加一个近乎免费的检查(审查建议)**:`incompatible` 数据**今天就有**(883 门有值),排课时顺手做软检查,避免把两门互斥课放进同一计划。

- 端点:`POST /api/sim/schedule {selected[], units_cap?}` → 上述结构。`units_map` 从 rules 树派生(复用 `simulator.DEFAULT_UNITS`,不重复定义常量)。
- 前端:6 列看板复用 `.course` 卡 + 调色板;未核实卡加 amber「学期未核实」标。
- **验收**:`prereq_map={}` 时每个传入码要么进某学期、要么进 `unplaced` 带 reason(零静默丢);每学期 ≤ cap;所有放置 `verified_offering=false` 且 warnings 含未核实计数。

---

## 阶段三b:先修课 prerequisite(独立后置,需重爬)

**软门(soft gate):课照样能选,但 `available()` 标 locked/unlocked,未知/不可解析 = 警告而非硬挡**(学生可乱序规划;skip 必须显式报告,不静默)。

### 3b.1 爬取(镜像 incompatible 解析块)

- `scraper.py`:`Course` 加 `prerequisite_raw: str=""` + `prerequisite_parsed: dict|None=None`;在 `parse()` 里定位 "Prerequisites" 块(和 incompatible 同页同结构),**注意与相邻的 "Recommended prerequisite" / "Companion" / "Assumed background" 区分**,别抓错块。保留**全文** raw(AND/OR/括号有意义)。
- 纯函数 `parse_prereq(raw) -> dict|None`:递归下降解析 `code | (expr) | expr (and|or|+) expr`(`+`=and,`or` 比 `and` 松,括号优先)。**任一未知 token(如 "8 units of…"/"Permission of…")或无法干净归约 → `{"op":"raw","unparsed":raw}`**(保守,绝不臆造结构);空 → `None`(无先修=解锁)。纯确定性,无 LLM。

解析树形态:
```
{"op":"and"|"or", "children":[node,...]} | {"op":"course","code":"CSSE1001"}
| {"op":"raw","unparsed":"<原文>"} | null   # null=无先修字段=默认解锁
# "CSSE1001 or ENGG1001" -> {op:or,[{op:course,CSSE1001},{op:course,ENGG1001}]}
```

### 3b.2 迁移 + 入库

- `build_db.py`:DDL 后加幂等 `ALTER TABLE courses ADD COLUMN IF NOT EXISTS prerequisite_raw TEXT / prerequisite_parsed JSONB`;`COLS`/`JSON_COLS` 加字段。**注意**:`prerequisite_parsed` 缺失要存 JSON **`null` 不是 `[]`**(`row_values` 当前把 None JSON 强转 `[]`,会污染「无先修 vs 空」语义)。
- 重爬 `scraper.py --file course_ids.txt`(1508 个 S1 id)回填,**只覆盖 S1**;爬完打印覆盖摘要 `scraped / with_prereq / no_prereq_field / unparseable`。

### 3b.3 引擎软门 + 缺口报告

```python
def satisfied(tree, selected) -> tuple[bool, str|None]:
    # None->(True,None); course->in selected; or->any; and->all; raw->(True,"unparseable:…")软警告
# PlanSimulator 加:
_load_prereqs(conn)          # self._prereq: dict[str,dict]
locked_status(code) -> {code, state:"unlocked"|"locked"|"unknown", prereq_raw, missing:[...], reason}
available_detailed() -> [{code,state,missing,reason}]   # 不隐藏 locked,只打标(新方法,不改 available())
prereq_report() -> {covered,no_prereq,satisfied,locked,unknown_unparseable,no_data_not_scraped, no_data_codes:[...]}
```

**覆盖上限显式报告**:2559 的 77 个码里 33 个 S2-only 永远爬不到 → `prereq_report()` 把这 33 个列进 `no_data_not_scraped`,**区分「parsed=null(确无先修)」与「未爬到(未知)」**,绝不把未爬到当「无先修」。

- **验收**:`parse_prereq("CSSE1001 or ENGG1001")` → or 树;`"Permission of Head of School"` → `{op:raw}`;无字段 → null;locked 课在其先修进 `.selected` 后转 unlocked;不可解析报 `state="unknown"`(警告)非 locked;DB 里 `parsed IS NULL` 数 == 无先修数。

---

## 阶段四:AI 选课建议(sim_advise.py,可选后置)

**原则:确定性代码定「能选什么」(`available()`),LLM 只在这个固定集合内排序 + 解释,永远不能引入集合外的码。** 双重护栏:① 候选集喂 LLM 前已 ∩ `available()`;② 复用 `answer.guard_citations` 二次剥离幻觉码。

```python
def advise(conn, program_id, state, goal) -> dict:
    sim = PlanSimulator(conn, program_id); 重放 state
    avail = set(sim.available())
    if not avail: return 固定消息          # 不调 LLM(复刻 answer.answer 空短路)
    rows = retrieval.semantic_search(conn, goal, k=40)   # 不能在 SQL 里过滤码集,Python 侧 ∩
    candidates = [r for r in rows if r["code"] in avail][:8]
    advice = answer.guard_citations(LLM(facts=build_facts(candidates), temp=0), candidates)
    return {candidates, advice, available_count, reachable_count, excluded_count, dropped_codes}
# 端点 POST /api/sim/advise {program_id, state:{selected,chosen_plans}, goal}
```

**诚实披露(审查重点,CLAUDE.md rule 19)**:2559 的 49 个 available 里只有 **28 门**有 embedding 可被 `semantic_search` 召回,另 21 门(S2-only)**永远进不了候选**——`dropped_codes` 覆盖不了它们(它们从没成为候选)。所以响应必须给出 `available_count` 和 `excluded_count`,明确告诉用户「N 门可选课本学期无数据、已排除」,而不是在可达子集上静默建议。另:`semantic_search` 默认 `min_sim=0.45` 的相似度地板可能让小众 goal 候选为空——召回不足时抬 k 或二次检索。

- **验收**:空 available → 固定消息、0 次 LLM;`candidates ⊆ available() ∩ 28 可达码`;把 LLM mock 成返回乱码(含编造码 ZZZZ9999),`candidates` 仍是 `available()` 合法子集、advice 里编造码被剥除并进 `dropped_codes`——**证明是规则引擎而非 LLM 决定可选性**;`temperature=0` 候选可复现。

---

## 阶段五:排课收紧(三b 完成后)

`scheduler.schedule()` 已把 `prereq_map` 作参数,三b 落地后只需把 `prerequisite_parsed` 拍平成 `code -> set(先修码)` 传进去,**无需重写**。此后排课才是真·先修合法的拓扑序;在此之前 warnings 始终标「先修未约束」。S2 offering 数据到位后再开 `offering_map` 强约束。

---

## 6. 决策(已拍板)

| # | 决策 | 定论 | 落点 |
|---|------|------|------|
| 1 | 持久化(进度追踪是否跨会话保存) | **localStorage 够**,不写库 / 不登录;多设备需求出现再加 `student_plan` 表 | 阶段二 §2.2 / §2.3 |
| 2 | 二选一(equivalence)分组卡 | **进 v1**,复用 index.html `.course.grp`/`.oror`,数据由 `available_by_rule` 的 equiv slot 提供 | 阶段一 §1.2 / 阶段二 §2.3 |
| 3 | AI 建议 goal 中文 | **直接喂 bge-m3**(多语言),不先翻译 | 阶段四 |
| 4 | API 代码放哪 | **内联进 `server.py`**(匹配现有扁平模块风格) | 阶段一 §1.5 |

其余原本模糊处已按审查建议定死并写进对应阶段:返回契约用分组形态(slot)、`available()` 不改签名(加 `available_by_rule()` 新方法)、错误码 404/400 不吞、program 范围 v1 只 2559、offering 约束默认关、排课加 incompatible 软检查。

> **演进决策(用户定)**:MVP 用 vanilla JS 单页跑通(复刻 index.html);验证后前端迁 **React.js**、后端继续 FastAPI——`/api/sim/*` 契约在 MVP 就冻结,React 版直接复用,无需重设计后端。

---

## 7. Roadmap

**MVP(先做这两个,vanilla JS + 现有 FastAPI):**
- [x] **阶段一 API**:`server.py` 内联 4 路由(`/sim` + `/api/sim/{programs,program/{id},state}`,无状态)+ `simulator.available_by_rule()`(course/equiv slot)+ `selected_by_rule()` + LEFT-join enrichment(`_hydrate`)+ 404/400 不吞错。**端到端断言全过**(335 programs / 2559=48u 6 规则 / 空态 49 可选 / equiv 收敛 / 错误码)。
- [x] **阶段二 Web UI**:`web/simulator.html` + `GET /sim`,复刻 index.html 视觉,规则段/进度条/major 单选/二选一卡/实时可选/localStorage。**无头浏览器验证 0 JS 错误、点选交互生效**。
- [x] **阶段二+ 时间表版 UI(重写)**:专业搜索框(335 个,客户端过滤)+ 双栏(左=按规则分组的可修课,右=年份×学期网格);**拖拽**左→右落位、右→右移动、× 移除、点卡自动放最早可开学期;**开课硬拦**(客户端按 `offerings` 判,S2-only 拖进 S1 格被拒)+ **先修(时序)/学分上限/互斥软提示红标**(`POST /api/sim/state` 加 `placement` → `_validate` 透出 `validation`/`offerings`);一键自动排(调 `/api/sim/schedule` 回填 placement)。Playwright 验证:搜索/拖放/硬拦/红标/自动排全过,0 JS 错误。

**MVP 之后(增强,只依赖 `/api/sim/*` 契约):**
- [x] **阶段三a 排课**:`scheduler.py`(贪心拓扑装箱,offering 默认关、incompatible 软检查、零静默丢)+ `POST /api/sim/schedule` + 前端「排课预览」学期看板。**离线 7 项 + 端到端断言全过**(顺序/上限/环/未核实/零丢)。
- [x] **阶段三b 先修**:**全完成并已回填上线**。`scraper.py` 加 `prerequisite_raw/parsed` + `parse_prereq`(AND/OR 树 + UQ 缩写码展开 `1111→ACCT1111`,非码条件→raw 软兜底);`build_db.py` 迁移(ALTER 两列,parsed 存 JSON null);引擎软门 `satisfied/locked_status/available_detailed/prereq_report`(只装真树/raw,排除 jsonb null);state 端点透出 `locks` + 前端「需先修 X」锁标(置灰仍可点,软门)。**重爬 1508 门 0 失败**;先修 535 真树 + 212 raw + 761 确无;DB `_prereq` 736 码。实测 CSSE2002 选 CSSE1001 前 locked→后 unlocked;2559 覆盖 30 有先修/8 确无/6 软未知/33 未回填(S2-only)。
- [x] **阶段五 排课收紧 + 开课学期核实**:① `schedule()` 吃真 `prereq_map`,先修顺序零违例;② **offering 强约束已开**——关键洞察:`s2_course_codes.txt`(2026:2 搜索页 2074 码)里出现即代表该课 S2 开,**零爬取**即得 offering map(S1 来自 courses.semester,S2 来自该清单;实测 6/6 与 course.html 开课表吻合)。`server.py` 加 `S2_CODES` + 排课端点建 `offering_map` 开启约束。实测 24 门 BCompSc 计划 **24/24 已核实**、8 门 S2-only 全钉在 S2 学期、零静默丢。tie-break 改为低年级优先(入门课落 Y1)。
- [ ] **阶段四 AI 建议**(可选,未做):`sim_advise.py`,确定性定池 + LLM 只排序(中文 goal 直喂 bge-m3)+ guard_citations 二次护栏 + 诚实披露可达池

**修订(用户反馈):**
- [x] **选修区间显示修复**:规则头从只显 `units_min`(误把"0/2"当上限)改成显 min–max(C.1 `需2–16`、C.2 `需4–22`、D/F `可选0–16`),进度条按上限填;D/F 不再误显示"已满足"。
- [x] **level-1 学分上限(数据驱动)**:`programs.aux_rules` 早已爬有 `level_cap`("at most 24 units at level 1",93 个程序有)。引擎 `level_cap_status()` 解析+实时算各级别已选学分,state 端点透出 `level_caps`,UI 总览显示 `L1 X/24`(超标红)。**24 来自数据,非写死**。
- [ ] **No-Major-Option(暂不做)**:官方"Major 16 **或** No-Major 8–24"的另一条路径,在 AppData 里只有 HTML `summaryDescription`(引用同一批 C.1/C.2 选修池),无结构化课表。干净抓取需解析 HTML 摘要 + 引擎建 Either/Or 分支,工程量大/脆弱/少数路径,性价比低,记录待定。
- [ ] **(a) 任意课填 D/F(待定)**:需课程搜索端点 `GET /api/sim/courses?q=` + UI 搜索框 + 引擎把"计划外已开课"计入通选/拓展(填到 max)。

**演进(MVP 验证后):**
- [ ] **前端迁 React.js**:规则段 / 进度条 / 排课看板组件化;后端 FastAPI 不变,复用已冻结的 `/api/sim/*`;localStorage 不够再加 `student_plan` 表 / 登录

> 关联:`.claude/plans/program_plan.md` 已把 `simulator.py` 引擎标为完成;本计划是其上的「暴露 + 增强」层。先修 / S2 覆盖与 `qa_accuracy_plan.md`、`s2_progress.md` 的数据扩展共享同一上游。
> **后续:剩余项((a) 任意课填 D/F、阶段四 AI 建议)及引擎修复已并入 `perfect_plan.md`,在那边推进。**
