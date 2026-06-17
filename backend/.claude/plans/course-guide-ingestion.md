# 课程攻略语料接入 — 计划

分支:待建(建议 `feature/course-guide-ingestion`)

## 背景与目标

用户手写的「课程攻略 / 选课经验」长文要接入系统。已用真实样本做过库内对账,结论先行:

- **COMP4500/7500 攻略**:把两门课当一门写,实测 3 处事实错 —— 先修(4500=COMP3506 / 7500=COMP7505)、
  两个作业权重(4500=20+20 / 7500=15+15)、期末权重(4500=50 / 7500=60),且都漏写「期末是 Hurdle」。
- **INFS7410 攻略**:事实层几乎满分(quiz10 / project10+30 / oral50 hurdle 全对)。
- 两篇都「认真写」,但事先无法区分谁对谁错 —— **唯一区分手段是拿结构化数据(`course_detail`)逐字段比**。

由此定下设计前提:**攻略只能当「经验层」补充,绝不能当事实源**;事实(权重/先修/Hurdle/日期)永远走
`course_detail`;攻略价值在 DB 没有的东西(口试是 in-person、AI 糊的代码会现形、guest lecture 爱考、
best-N-of-M、避坑建议)。

目标:
1. 让攻略的**经验层**可被检索并答出,答案强制带「20XX 年个人经验、非官方、事实以课程大纲为准」口径 + 官方课程页链接。
2. 入库前用**对账闸门**把攻略声明的事实逐项比 `course_detail`,不一致就**拒绝入库 + 列冲突**(防 COMP 那类错进库)。
3. 事实类问题(先修/考核占比/考试日期)**永不**召回攻略,仍走 `course_detail` / 官方 KB。

非目标(不在本计划内):
- 不动 simulator;不改 course / program / 官方 KB 的事实答案语义;不放宽任何 student-facing 红线。
- **「选课搭配图」不进 RAG** —— 它本质是 simulator 输出(program_id + 一组选课),应做成 simulator 命名预设,
  另立计划。本计划只处理散文攻略。

## 前提

- **已确认**:`course_detail`(`retrieval.py:435`)是事实权威源;三门课已实测对账。
- **已确认**:库内三门均为 `year=2025, S2`,现服务即滞后一周期 → 年份口径强制,不是可选。
- **已确认**:embedding 走本地 Ollama bge-m3 1024 维(`embed.py`),与 courses / kb_chunks 同一向量空间,攻略复用。
- **已确认(案 A)**:攻略**新建独立 `course_guides` 表**,不复用 `kb_chunks`。理由是结构性隔离——事实查询物理上不可能命中攻略,见 §设计 0。下文爆炸半径与落地顺序均按案 A。

## 设计

### 0. 存储:案 A —— 新建 `course_guides` 表(已定)

核心约束:`kb_search`(`retrieval.py:389`)**当前不按 source_type 过滤**,且有**两条路**进 kb ——
planner 直接分类(`qa.py:506`)+ 课程检索弱/空时的兜底(`qa.py:465-484`)。攻略若与官方 KB 同表,
这两条路都会把攻略漏进事实答案。**因此采用案 A 物理隔离**(案 B 复用 kb_chunks 的对比附在末尾,仅留作决策记录)。

**案 A(已采用):新建 `course_guides` 表**
```sql
CREATE TABLE course_guides (
    id            TEXT PRIMARY KEY,   -- {code}_{year}-{chunk_idx}
    course_code   TEXT NOT NULL,
    year          INTEGER NOT NULL,
    semester      TEXT,
    section       TEXT,               -- 来自 ## 小节标题
    text          TEXT NOT NULL,
    source        TEXT,               -- 来源标注(匿名学长帖…)
    profile_url   TEXT,               -- 官方课程页(答案携带的可验证链接)
    checked_at    TEXT,               -- 最近对账日期
    embedding     VECTOR(1024)
);
CREATE INDEX idx_course_guides_code ON course_guides(course_code);
CREATE INDEX idx_course_guides_emb  ON course_guides USING hnsw (embedding vector_cosine_ops);
```
- pros:**事实查询物理上不可能命中攻略**(零泄漏,最契合 student-facing 红线 1/3);`course_code`+`year`
  是一等列,过滤/降权直接;`kb_search` / 那 4 个 eval CLI(build_kb_vocab / threshold_scan / rerank_probe /
  answerability_eval)**完全不用动**,官方 KB 的阈值与词表不被攻略文本污染。
- cons:多一张表 + 一个 `guide_search` 函数(但 embed/索引逻辑可复用,增量小)。

> **决策(已定):取案 A。** 理由:这套系统的中心设计就是「攻略绝不覆盖事实」,物理隔离把它变成结构性保证而非纪律性约束;
> 多写的一张表/一个函数,远小于「每处 kb_chunks 查询都要记得过滤」的长期心智负担。下文爆炸半径与落地顺序均按案 A。

**案 B(未采用,仅留决策记录):复用 `kb_chunks` + `source_type='guide'`**(表已有 `source_type` 列,`kb_build.py:31`)
- pros:少一张表,复用现成入库/检索管线。
- cons(否决理由):**默认泄漏**。必须改 `kb_search` 加 `source_types` 参数,把官方路径**显式**约束成
  `source_type IN ('kb_article','kb_faq')`,且两条 kb 路(`qa.py:506` 与 `qa.py:465-484` 兜底)都要传对;
  build_kb_vocab / threshold_scan 的全表 `SELECT ... FROM kb_chunks` 会把攻略文本算进官方词表/阈值,需各自加过滤。
  **任何未来新增的 kb_chunks 查询忘了加过滤 = 一次静默泄漏**(规则 19 的反面),长期维护风险高。

### 1. 语料格式 —— `data/guides/<code>_<year>.md`

frontmatter 放**可机器对账的事实**,正文只放**无法结构化的经验**(按 `##` 分节 = 分块边界):

```markdown
---
course_code: INFS7410
year: 2025
semester: S2
source: 学长经验贴(匿名)
nature: subjective
claims:                       # ← 对账闸门读这里,逐项比 course_detail
  prereq: "INFS2200 or INFS7903"
  assessment:
    - {name: Quiz, weight: 10}
    - {name: Project Part 1, weight: 10}
    - {name: Project Part 2, weight: 30}
    - {name: Final Oral Exam, weight: 50, hurdle: true}
checked: ""                    # 对账通过后由脚本回填日期
---

## 这门课讲什么
检索、重排(re-ranking)、RAG 三块……

## 考核形式(经验,以当年 ECP 为准)
期末是 in-person 口试、要核身份、Hurdle 不过直接挂科,2025 年在 11 月那两周……

## 避坑与准备
口试才是正餐:assignment 是体力活,口试当面证明代码是你写的、原理你真懂;
guest lecture 很容易考到,pra 别划水……
```

**作者硬规则(写进 `data/guides/README.md`)**:
1. 一课一篇一年 —— COMP4500 / COMP7500 拆两个文件(先修、权重都不同)。
2. 事实进 `claims`,正文不写裸权重/裸先修当事实;要提就带「(以当年 ECP 为准)」。
3. 裸日期删或显式标年:「11 月 10–21 日」→「2025 年 11 月那两周」。
4. `nature: subjective` + `source` 必填(答案渲染「据 20XX 经验」用)。
5. 写完跑对账,通过后 `checked` 才有值。

### 2. 对账闸门 —— `pipelines/guide_check.py`(入库前置,确定性)

输入一篇 `<code>_<year>.md`,拉 `course_detail(conn, code)`,把 `claims` 逐项比对:
- `prereq` 比 `prerequisite_raw`(字符串归一后比对)。
- `assessment[].weight` 按类别比 `assessments[].weight`;`hurdle` 比对应项 `hurdle`。
- 任一不一致 → 打印冲突表 + **非零退出**(当入库闸门,规则 19:不静默放过)。
- 全部一致 → 回填 `checked` 日期,放行。

COMP4500_2025.md 会当场报「期末漏标 hurdle」;若有人误填 7500 的数到 4500,报权重冲突。

### 3. 入库管线 —— `pipelines/guide_build.py`

1. 遍历 `data/guides/*.md`,逐篇先跑 `guide_check`(不过则 skip + 计数,**汇总报告跳过数与原因**,规则 19)。
2. 解析 frontmatter + 按 `##` 切块(复用 `kb_parse.sections_from_markdown`,`kb_parse.py:103`)。
3. 每块 `embed(text)`(复用 `embed.py:22` 的 bge-m3 helper)。
4. `profile_url = COURSE_PROFILE_URL.format(code)`(复用 `retrieval.py:37`)。
5. upsert 进 `course_guides`(id = `{code}_{year}-{idx}`,可重复跑)。
6. 末尾打印:入库块数 / 跳过篇数+原因 / 各 code 的对账日期。

### 4. 检索 —— `retrieval.guide_search`

```python
def guide_search(conn, course_code: str, query: str, k: int = 4, min_sim: float = 0.55) -> list[dict]:
    # source 物理隔离:只查 course_guides,且必须带 course_code 过滤
    # 向量同 kb_search:bge-m3,min_sim 兜底(弱召回宁可空,红线 3)
```
- 强制 `WHERE course_code=%s`:攻略是课程范围内的,跨课不召回。
- `min_sim` 初值 0.55,上线前用真实问题扫一遍(可复用 `threshold_scan` 思路),别拍脑袋。
- **不改 `kb_search`**(案 A 下官方 KB 路径零改动)。

### 5. 路由 —— planner 新增 `guide` mode

`course_detail` 已按「问题含课程码 + 问这门课本身」路由(`planner.py:895`)。`guide` 与它同样**以课程码为锚**,
靠**经验意图**区分,确定性正则,不交给 LLM 拍板(红线 / 规则 12):

```python
_GUIDE_INTENT = re.compile(r"难不难|好过吗|水不水|怎么样|体验|值不值|踩坑|避坑|怎么准备|给点建议|经验|攻略", re.I)
```
- 命中 `_GUIDE_INTENT` 且抽到 course_code → `mode="guide"`,`course_code` 透传。
- **事实意图优先级更高**:同一句若同时命中 `_DATE_INTENT`(`qa.py:37`)或先修/考核词,**走 course_detail/kb,不走 guide**
  (事实问题永不进攻略,这是红线)。
- qa 里 `guide` 分支:`guide_search(conn, code, q)`;**库内该 code 无攻略 → 回退 course_detail**
  (优雅降级,不报错、不空答)。

### 6. 答案 —— `answer.answer_guide`

- LLM 只**摘要/转述经验层**,被 `guide_search` 召回的块 grounding,citation guard 复用(`answer.py` 现成)。
- **强制前缀**(代码拼,非 LLM 生成,确定性):
  `> 以下为个人经验({year} 年),非 UQ 官方;先修、考核占比、考试日期等请以课程大纲为准:{profile_url}`
- 官方链接 = `course_guides.profile_url`(红线 2:每个答案可一键回官方核验)。
- `checked` 距今超一个学期 → 追加「该经验可能已过期,务必核对当年大纲」。

## 爆炸半径(案 A;file-by-file)

### 新增
- `data/guides/`(README + 攻略 md)、`pipelines/guide_check.py`、`pipelines/guide_build.py`。
- `retrieval.py`:`guide_search`(+ `GUIDE_COLS/KEYS`),建表 DDL(随 guide_build 或并入 build_db,二选一,别两处)。
- `services/answer.py`:`answer_guide` / `answer_guide_stream`(对照 `answer_course_detail` L598/L618 的结构)。

### 改动
- `planner.py`:`MODES`(L154)加 `"guide"`;PROMPT 不必改(guide 由代码侧 `_GUIDE_INTENT` 确定性判定,
  避免弱模型抖动);course_code 抽取沿用现有。新增 `_GUIDE_INTENT` + 判定函数,**事实意图优先**短路。
- `qa.py`:
  - `run`(分类后)+ 流式分支(L582 / L635 / L655 同构处)加 `mode=="guide"` 分支 → `guide_search` →
    `answer_guide`;无攻略回退 `course_detail`。
  - 模块 docstring(L10 那段 mode 列表)同步加 `guide`(中文 docstring 约定,规则 18)。
- `core/config.py`:无需改(DSN/DATA_DIR 已够);若加阈值常量(GUIDE_MIN_SIM)放 planner/retrieval 顶部,与 KB_*_SIM 一致风格。

### 不动(案 A 的价值)
- `kb_search` / `kb_build` / `kb_parse`(仅 `sections_from_markdown` 被 import 复用,不改它)/
  build_kb_vocab / threshold_scan / rerank_probe / answerability_eval —— **官方 KB 全链零改动、零污染**。

## 落地顺序(每步可跑、可回滚;规则 17)

1. **建表 + `guide_check`**,先不入库。手动喂 INFS7410 / COMP4500 / COMP7500 三篇,
   验证 COMP4500 报 hurdle 冲突、INFS7410 通过。**Checkpoint**:对账逻辑正确(这是闸门,必须先硬)。
2. **`guide_build` 入库**三篇(COMP4500 改对后)。查 `course_guides` 行数 / 各 code 块数 / 对账日期。**Checkpoint**。
3. **`guide_search` + 单测**:`guide_search(conn,"INFS7410","口试怎么准备")` 召回经验块且 `course_code` 全为 INFS7410;
   `guide_search(conn,"COMP4500","难不难")` 不串到别的课。**Checkpoint**。
4. **接线 planner + qa + answer_guide**:`mode=="guide"` 端到端;`_GUIDE_INTENT` 命中 / 事实意图优先 / 无攻略回退
   course_detail 三条路各一个用例。验证答案带年份前缀 + 官方链接。**Checkpoint**。
5. **小规模评测后再扩**:补 5–10 门高价值课;按红线 6 跑一组真实问题人工核对(不是「没崩」)。

## Risks(红线面)

1. **攻略漏进事实答案**(最高风险):案 A 物理隔离已堵;**绝不可**为「顺手」让 `kb_search` 或兜底路去查 `course_guides`。
   事实意图优先短路(§5)必须有测试覆盖:「INFS7410 先修是什么」走 course_detail、绝不出攻略文本。
2. **过期经验当当前**:`checked` 过期提示 + 年份前缀是软防护;裸日期在语料阶段就该删(§1 规则 3)。脚本可在 `guide_check`
   里扫正文中疑似具体日期(`\d{1,2}\s*月`)→ warn,逼作者标年。
3. **对账假过**(规则 16):`guide_check` 比的是**值**不是「字段存在」—— weight 必须数值相等、prereq 归一后字符串相等、
   hurdle 布尔相等;弱比对(只查 claims 有没有这个 key)等于没闸门。
4. **回退静默成空**:无攻略回退 course_detail,若该 code 也不在库 → 复用 course_detail 既有的「优雅提示」,不能空答(红线 3)。
5. **embed 维度/空间漂移**:必须用 `embed.py` 同一 bge-m3 路径;换模型/换维度会让 guide 向量与库不可比 → 召回乱。

## 迭代预算(规则 13)
- 每个 Checkpoint 最多 3 轮调试;对账/召回不达标 2 轮内修不回 → 停下回滚到上一 Checkpoint,带现状汇报。
- 上线前那组真实问题人工核对未过 → 不扩量、不放行(红线 6,「差不多」不算过)。

## 另立计划(本计划外)
- **选课搭配 → simulator 命名预设**:存 `program_id + 选课列表` 为模板,用现有 sim UI 渲染,确定性、永远当前、可 fork。
  与本计划解耦,单独提案。
