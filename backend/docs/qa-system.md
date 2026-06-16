# UQ 课程问答系统 — 现状与技术文档

> 适用范围:RAG 问答这条产品线(`/api/ask`、`/api/ask/stream`)。选课模拟器
> (`/api/sim/*`)是同一后端里的另一条产品线,本文只在「整体架构」一节带过,细节见
> `app/services/simulator.py` 与相关记忆。
>
> 最后核对代码:2026-06,基于当前工作区(含未提交改动)。

---

## 1. 这是什么

面向**真实 UQ 学生**的课程助手,回答选课/培养方案/学校事务类问题。核心定位是
「帮你找到并看懂官方信息」,不是「我就是权威」——所以每个答案尽量带官方来源链接,
高风险事实(先修、census date、费用、截止日期)一律走确定性数据,不让 LLM 自由发挥。

整个问答系统围绕一条贯穿全局的设计原则展开:

> **确定性的事用代码,语言的事才交给 LLM。**
> 路由、阈值、护栏、先修、费用、日期——凡是「同样输入永远同样输出」的,都是显式代码
> (条件判断 / 配置值 / 查表);LLM 只做分类、起草措辞、消解歧义,**绝不决定高风险事实**。

这条原则对应 `backend/.claude/rules/student-facing.md` 的 7 条红线,其中最关键的三条:

1. **高成本答案来自确定性数据,不来自 LLM 自由生成**(先修/census/费用/退课影响/考试日期)。
2. **每个答案都带官方来源 URL**,让学生一键核对。
3. **Refuse over wrong**:召回弱或高风险且未命中时,宁可说「不确定 + 给官方链接」,不给自信的猜测。

---

## 2. 技术栈与运行形态

| 层 | 选型 |
| --- | --- |
| 后端 | FastAPI(Python 3.13),只提供 JSON API,**不托管 HTML** |
| 前端 | 独立的 Vite + React 19 + TypeScript 应用(HeroUI),dev 下 `/api` 代理到 `127.0.0.1:8077` |
| 数据库 | Postgres + pgvector,端口 **5433**,库名 `uq_courses` |
| 向量 | 本地 Ollama `bge-m3`(1024 维),HNSW 索引 |
| 生成/规划 LLM | 默认本地 Ollama `qwen2.5-coder:7b`;设了 `DEEPSEEK_API_KEY` 则 planner + answer 全程走 DeepSeek |

LLM 后端可插拔,逻辑集中在 `app/services/llm.py`:

- 设了 `DEEPSEEK_API_KEY` → 走 DeepSeek(`deepseek-chat`)
- 没设 → 本地 Ollama
- `LLM_ENABLED=false` → 即使有 key 也强制本地(临时回退用)
- `.env`(`backend/.env`)在 import llm 时自动加载;真实环境变量优先,不被 `.env` 覆盖

---

## 3. 整体架构

### 3.1 分层

严格单向:`api → services → core`,CLI 落在 `scrapers/` 与 `pipelines/`。统一通过包导入
(`from app.services import qa`),不用相对导入。

```
app/
  main.py              只组装路由 + 启动(启动时建一次 FTS 索引)
  api/
    ask.py             问答路由(本文主角)
    sim.py             选课模拟器路由
  services/
    qa.py              问答总入口:planner → 路由 → answer
    planner.py         自然语言 → 查询计划(LLM 分类 + 大量确定性纠偏)
    retrieval.py       统一检索层(filter / semantic / hybrid / kb)+ SQL 安全网
    program_lookup.py  课程↔专业关系的纯 SQL 查询
    answer.py          grounded 答案生成 + 引用护栏 + KB / 单课答案
    answerability.py   KB 可答性门(P0 确定性 + P2 LLM),防虚构实体编答案
    reranker.py        可选 cross-encoder 重排(默认关,仅留架构位 P1)
    llm.py             可插拔 LLM 后端(Ollama / DeepSeek)
    simulator.py       选课模拟器规则引擎(另一条产品线)
  core/
    config.py          DSN / DATA_DIR / S2_CODES 的唯一配置源
```

### 3.2 两条产品线

- **问答(RAG)**:`/api/ask`、`/api/ask/stream`。本文重点。
- **选课模拟器**:`/api/sim/*`。完全确定性,状态计算无 LLM;客户端持有完整 state,
  服务端每次无状态重放。唯一带 LLM 的路径是 `sim_advise.py`(候选池确定性,LLM 只排序+解释)。

---

## 4. 数据层

### 4.1 数据库表

| 表 | 用途 | 关键列 |
| --- | --- | --- |
| `courses` | 每行一门课的一个 offering(同课多学期=多行) | `offering_id`(PK)、`code`、`title`、`semester`、`year`、`location`、`attendance_mode`、`level`、`units`、`coordinating_unit`、`has_exam`、`has_hurdle`、`midterm_status`、`group_status`、`course_type`、`assessments`(JSONB)、`prerequisite_raw`、`description`、`search_blob`、`embedding`(vector) |
| `programs` | 专业 | `program_id`(PK)、`title`、`total_units`、`rules`(JSONB) |
| `program_course` | 专业–课程扁平关系 | `program_id`、`course_code`、`requirement_type`('core'/'elective')、`course_list`、`via_plan`、`plan_subtype`、`equiv_group` |
| `program_exclude` | 专业无条件禁修的课 | `(program_id, course_code)` PK |
| `kb_chunks` | 知识库切片(FAQ / 学术日历 / 政策) | `id`(PK)、`url`、`source_type`、`page_title`、`breadcrumb`、`h2`/`h3`、`text`、`lastmod`、`fetched_at`、`embedding`(vector) |

> 几个对问答行为关键的字段约定:
> - `midterm_status` / `group_status` 是**三态**:`has` / `none` / `unknown`。`unknown` =
>   判不出,绝不计入「没有 X」,但答案会显式提示有多少门未计入(规则 19,不静默漏)。
> - S1 开课看 `courses.semester`;S2 开课看 `S2_CODES`(`config.py` 从
>   `data/s2_course_codes.txt` 加载,因为 `semester` 列里 S2 不全)。
> - `code`(课程码)是**文本列,严禁进 WHERE**——学科横跨多个码、课名是英文,文本 LIKE 必错;
>   主题一律走语义,「按年级筛」改用「课码首位数字」在 Python 层后过滤。

### 4.2 数据来源(离线管线,有序)

爬虫产 JSONL,管线灌库:

1. `scrapers/collect_ids.py` → `scrapers/scraper.py` → 课程 JSONL
2. `pipelines/build_db.py` — 建表 + 灌 `courses`(embedding 先留空)
3. `pipelines/embed.py` — 本地 Ollama bge-m3 填 embedding(1024 维)+ 建 HNSW
4. `scrapers/program_scraper.py` → `pipelines/build_programs.py` — 专业 + 规则
5. `scrapers/scrape_aux_rules.py` → `pipelines/build_aux.py` — 禁课表 / 附加规则
6. 知识库:`kb_discover` → `kb_fetch*` → `kb_parse` → `kb_build`(切片 + embed)
7. `pipelines/build_kb_vocab.py` — 产 `data/kb/kb_vocab.txt`(answerability 缺席判定用词表)

> 数据新鲜度是生命线(红线 4):`pipelines/watch_s2.py` + launchd 每天监听 profile 上线,
> 出一批入一批,学期初(2 月 / 7 月)更勤。

---

## 5. 问答主流程

入口 `qa.run` / `qa.run_stream`,内部共用 `_retrieve()` 做检索 + 路由,只在生成层分叉。

```
问题
 └─ planner.plan()  ──分类──▶ mode
      ├ filter        retrieval.filter_search           结构化 WHERE
      ├ semantic      retrieval.semantic_search         向量 + 全文 RRF
      ├ hybrid        retrieval.hybrid_search           结构化 + 语义
      ├ program       program_lookup(c2p / p2c / permit) 确定性枚举,不走 LLM
      ├ course_detail retrieval.course_detail           单课结构化详情
      ├ kb            retrieval.kb_search + answerability 知识库 FAQ/日期/政策
      └ (empty)       qa 层兜底:问题太宽泛,固定提示句
                                  │
                       ┌──────────┴──────────┐
                       │  生成层(answer.py)  │
                       │  只有 semantic /     │
                       │  hybrid / course_    │
                       │  detail / kb 走 LLM  │
                       │  且全部 grounded     │
                       └─────────────────────┘
```

只有 **semantic / hybrid / course_detail / kb** 四种会经过 LLM 生成,且都 grounded 在检索行 +
引用护栏(`guard_citations`)。**program / filter(低负担)/ empty 全是确定性答案,不碰 LLM。**

### 5.1 planner —— 规划器(`planner.py`)

LLM 只做语言活:判 mode、写 WHERE、给英文 `semantic_query`、抽 `course_code` / `program_name` /
`direction` / `kb_query` / `coord_unit`。其余全是确定性代码:

- **schema 实时注入**:`build_schema_doc(conn)` 把低基数列(semester / location /
  attendance_mode / level)的真实 distinct 值、以及约 31 个学院的 `coordinating_unit` 清单
  注入 prompt,让 LLM「从闭集里选」而非自由生成(院名都是 UQ 内部缩写,自由生成必拼错)。
  同时把枚举写进模块级 `_ENUM_CACHE` 供确定性兜底校验。
- **WHERE 合法性拦截**(`_clean_where`):剥离字符串字面量后逐 token 校验,只允许白名单列
  (`semester/year/location/attendance_mode/level/units/has_exam/has_hurdle/course_type/midterm_status/group_status`)
  + 逻辑词;出现文本列 / LIKE / SELECT / 脑补列一律整段清空。
- **确定性纠偏 / 兜底**(LLM 路由会抖动,这些是稳定保险):
  - **program 强制路由** `_force_program_route`:课码 + 关系词 → c2p;学位全名 + 课型词 →
    p2c;课码 + 学位名 + 「能否修」→ permit。
  - **course_detail**:课码 + 无学位名 + 无关系词 → 单课详情。
  - **校区/授课模式枚举守卫** `_enforce_enum_guard` / `_enforce_attendance_guard`:用户问
    非枚举值(Gatton / 线上)时,强制用**用户原字面值**让结果正确为空,**绝不被 LLM 换成
    St Lucia / In Person**(否则把全库面授课当线上课返回,confidently wrong,踩红线 3)。
  - **level 守卫** `_enforce_level_hint`:研究生/本科/master/bachelor → 强制对应字面值。
  - **学院范围** `_faculty_units` + Option C `_validate_coord_unit`:商科/文科查表 + LLM 选的
    院名(逐字命中真实枚举才放行),都走参数化 SQL,文本列不进 WHERE。
  - **快路径**:「低负担/躺平/水课」意图先于 LLM 短路——主观难度无数据(红线 1,绝不让 LLM 编
    难度),只映射成客观负担过滤(无考试 + 无 hurdle + 排除 thesis/research/placement)+ 按
    考核项数升序。
  - **跨学期合取** `_both_semesters_intent`:「S1 和 S2 都…」走 `filter_search_both_semesters`
    (`GROUP BY code HAVING count(DISTINCT semester)=2`),避免扁平 IN 把并集当合取、数量虚高。
  - **课码首位筛年级** `_code_level_digits`:「1 或 3 开头」抽成数字集,qa 层 Python 后过滤。
  - **课程性质排除** `_excluded_title_kw`:capstone/project/review/proposal 等 `course_type`
    分不出的,走参数化 NOT ILIKE。

planner 产出 `mode ∈ {filter, semantic, hybrid, program, kb, course_detail}`;无法形成检索条件时
抛 `ValueError`,由 `qa._retrieve` 先试 KB、再优雅兜底成 `empty`。

### 5.2 retrieval —— 检索层(`retrieval.py`)

全部确定性,不调 LLM。四种检索 + SQL 安全网:

- **`guard_where`**:SELECT-only 白名单。WHERE 只允许「白名单列 运算符 字面量」用 AND/OR
  连接,禁括号(IN 列表除外)/函数/逗号/子查询/SELECT/非 ASCII。不符合一律 `raise`。
  这是硬安全网,即使 planner 的 `_clean_where` 漏了也兜得住。
- **`filter_search`**:纯结构化过滤。`order_by` 走 `_ORDER_BY` 白名单(绝不拼用户串);
  `coord_units` / `exclude_title` 走参数化追加;同课跨学期按 code 去重。
- **`semantic_search` / `hybrid_search`**:向量 + 全文 **RRF 融合**(`RRF_K=60`)。融合是为了让
  「课名就叫 Machine Learning」这种被全文命中的课能排上来。向量召回卡 `SEMANTIC_MIN_SIM=0.50`
  (含纯全文命中也卡),低于此当噪声丢——这是实测出的分界(真主题课最低约 0.515,噪声多在
  <0.50)。残留的 0.50~0.55 off-topic 是 bi-encoder 固有局限,归 reranker(P1)。
- **`kb_search`**:知识库 chunk 语义检索,`min_sim` 默认 **0.62**(由 `threshold_scan` 在带负样本
  评测集上扫出)。低于阈值一律滤掉(弱召回拒答,红线 3)。**跨语言增强**:planner 在 kb 模式
  顺带产出英文 `kb_query`,`kb_search` 对每个 chunk 取 `max(sim_中, sim_英)` 召回,修「语料是
  英文、中文 query 贴阈抖动」的根因,而不是下调硬阈值放水。
- **`course_detail`**:单课完整详情,聚合同课多 offering(内容字段取最新行,开课学期/校区汇总)。

每行结果都带 `profile_url`(官方课程页链接,红线 2)。

### 5.3 program_lookup —— 课程↔专业(`program_lookup.py`)

纯 SQL,零 LLM,**enrolment 事实零幻觉**。qa.py 里 `_ans_c2p` / `_ans_p2c` / `_ans_permit` 把
查询结果**用代码枚举成中文答案**:

- **course_to_programs**:某课是哪些专业的必修/选修。区分三态:真·必修 / 二选一核心
  (equivalence 组,可换等价课)/ 选修;并标注哪些专业明确禁修该课。
- **program_to_courses**:某专业要修哪些课。带方向结构的专业(有 major/field)走 simulator
  规则引擎按方向完整枚举(覆盖 major 门控的课),否则扁平枚举;可叠加「专业范围内」的结构化
  筛选(组合查询);末尾补禁课提示。
- **permit**:某专业能否修某课(基于 `program_exclude` 禁课表)。

`find_program` 的排序是确定性的:精确同名 > 标题以该名开头 > 更短(更具体)> 字母序——
保证 `progs[0]` 选到独立专业而非组合专业。

### 5.4 answer —— 生成层(`answer.py`)

只服务 semantic / hybrid / course_detail / kb,全部 grounded:

- **`answer` / `answer_stream`**:把检索行确定性序列化成「事实清单」(`build_facts`,写明命中
  总数避免静默截断),喂 LLM(temperature 0)。system prompt 硬约束:只依据事实、每门课带课码、
  不编造、≤6 句。
- **引用护栏 `guard_citations`**:逐行剔除回答里**输入课程集合之外**的课程码(疑似虚构),
  并在末尾列出被剔除的码(不静默)。
- **主题相关性诚实**(topical=semantic/hybrid):召回可能全是语义噪声(如「游戏开发」召回生物课);
  让 LLM 判 top 召回是否真讲该主题,全噪声时给诚实声明而非自信当「X 课」列(软兜底,仍列最接近结果)。
- **KB 答案 `answer_kb*`**:
  - 高风险高频主题(目前 census date)走**确定性模板** `fixed_kb_body`(靠 top chunk 的
    `page_title` 触发,不写死会变的具体日期,红线 1)。
  - 否则 LLM grounded 生成 → 空答检测 → 重试(强指令 + 升温)→ 仍空则确定性降级到「看官方页面」
    (学生永远看不到「暂无信息」)。
  - 来源块 `kb_sources_block` 按 url 去重,由 qa 收尾确定性追加,**保证 100% 带官方链接**。
- **单课答案 `answer_course_detail*`**:先修/考核/学分/开课/「有没有某类考核」等**子问题走结构化
  确定性答案**(红线 1:先修的 and/or 逻辑、考核权重数字绝不让 LLM 改写);只有「讲什么/适合谁」
  这类长尾才回退 LLM grounded 简介。

### 5.5 answerability —— KB 可答性门(`answerability.py`)

KB 是增强兜底层,这道门守住「虚构实体问题(火星交换生 / 哈利波特学院)不要拿通用官方页编一套」,
同时**绝不误拒真学生的真问题**(红线 3 的「误拒=0」优先):

- **P0 确定性门 `answerable`**(零 LLM、零 TTFT):
  - **年份越界**:问题出现 `[2020, 2028]` 之外的学年(如 2099)→ 拒。
  - **英文实体缺席**:问题里的英文实体词在「召回 chunk 文本 ∪ 全语料词表」里全缺席 → 拒。
    只对英文做缺席判定——语料几乎全英文,对中文做会把所有中文真问题误拒。
  - 词表 `data/kb/kb_vocab.txt` 缺失则**抛错**,不静默当空集(否则缺席判定恒真、批量误拒)。
- **P2 LLM 门 `llm_answerable`**:确定性门放行后再过一次 LLM 分类(只判可答/不可答,补中文虚构
  实体)。**fail-open**:LLM 抖动/解析失败一律放行(误拒比漏拒更伤);开关 `KB_LLM_GATE=0` 可关。

### 5.6 reranker —— 可选重排(`reranker.py`,默认关)

cross-encoder 重排骨架(P1)。`KB_RERANK` 未设则永不 import torch,行为与无重排逐字节一致。
**边界**:reranker 只改 chunk 顺序/取舍,**绝不参与拒答**(拒答归 P0)。实测它「治召回不治拒答」
(火星 0.951 仍高于真问题),故 16GB 本机默认关,不进 student-facing 主链路。详见
`docs/rerank_answerability_findings.md`。

---

## 6. 确定性 vs LLM 边界(全项目不变量)

一张表看清「谁说了算」:

| 决策 | 谁定 | 在哪 |
| --- | --- | --- |
| mode 分类 / WHERE 草稿 / 英文主题词 | LLM | `planner` prompt |
| 路由纠偏 / 阈值 / 枚举守卫 / 学院映射 | 代码 | `planner` 确定性段 |
| WHERE 合法性(注入防护) | 代码 | `retrieval.guard_where` |
| 先修 / 学分 / 考核 / 开课(单课) | 代码 | `answer.detail_structured_answer` |
| 课程↔专业 / 禁修(enrolment 事实) | 代码 | `program_lookup` + `_ans_*` |
| census date 等高风险高频主题 | 代码模板 | `answer.fixed_kb_body` |
| 拒答(虚构实体 / 弱召回 / 年份越界) | 代码 | `kb_search` min_sim + `answerability` P0 |
| 引用越界剔除 | 代码 | `answer.guard_citations` |
| 把事实组织成自然中文 / 长尾课程简介 | LLM | `answer.*`(grounded) |
| 中文虚构实体二次判别 | LLM(fail-open) | `answerability.llm_answerable` |

---

## 7. API 契约

### `POST /api/ask`

请求 `{ "question": str, "generate": bool=true }`。响应(`AskResult`):

```jsonc
{
  "mode": "filter|semantic|hybrid|program|kb|course_detail|empty",
  "answer": "string | null",
  "courses": [{ code, title, units, level, semester, has_exam,
                requirement_type, equiv_group, course_list, sim, profile_url }],
  // course_to_programs 时是数组;program_to_courses / permit 时是单对象;空时 null
  "program_facts": "ProgramFact[] | ProgramAnswer | null",
  "chunks": [{ url, page_title, breadcrumb, source_type }],   // kb 模式
  "course": "CourseDetail | null",                            // course_detail 模式
  "meta": "string",        // 内部路由说明(调试用)
  "gen_context": ["..."]   // 实际喂给 LLM 的检索上下文(评测/调试,与生产同源)
}
```

错误不吞:`{ "error": "ExceptionType: message" }` + 对应 status code。

### `POST /api/ask/stream`(SSE)

逐行 `data: {"type": "...", "data": ...}\n\n`,顺序:

1. `meta` —— 一次,结构化课程 / program_facts / chunks / course
2. `token` —— 多次,答案增量(program / 低负担 / census 等确定性答案单块发)
3. `done` —— 一次,护栏后的完整答案

前端 `frontend/src/api/ask.ts` 的 `fetchAsk` / `fetchAskStream` 与上述契约一一对应,
类型定义(`AskResult` / `Course` / `ProgramFact` / `KbChunk` / `CourseDetail`)即接口文档。

---

## 8. 评测体系

两套互补,跑评测是「上线服务真实学生前」的硬门(红线 6,不是「没崩=通过」):

### 8.1 确定性评测(`app/pipelines/`,断言正确性)

| 脚本 | 评什么 | 固定集 |
| --- | --- | --- |
| `route_eval.py` | planner 路由准确率 | `data/eval/routing.jsonl`(110 条) |
| `answer_eval.py` | 端到端答案正确性 | `data/eval/answers.jsonl`(40 条) |
| `answerability_eval.py` | KB 拒答门(误拒/漏拒) | `data/eval/kb_refuse.jsonl`(68 条) |
| `kb_eval.py` | KB 召回 | `data/kb/golden.jsonl` |
| `relevance_scan.py` / `threshold_scan.py` / `floor_scan.py` | sim 阈值标定 | `data/eval/course_relevance.jsonl`(21 条) |
| `llm_judge_eval.py` | LLM judge 答案质量 | — |

> 已知缺陷(见记忆 `qa-eval-harnesses`):虚构实体在课程库一侧仍可能不拒答;`route_eval`
> 的 `where_has` 只校验列名不校验值。

### 8.2 LLM-as-judge 评测(`eval/`,量化质量)

对 `/api/ask` 跑 faithfulness / relevancy / context precision,与上面的确定性断言互补。

- 两套 judge 框架交叉验证,共用 `eval/generate.py` 产的样本(`questions.jsonl`,44 条):
  - **RAGAS**(`ragas_*.py`):judge=DeepSeek,embedding=本地 bge-m3。
  - **DeepEval**(`deepeval_*.py`):judge=DeepSeek 纯 LLM 判分。
- 两者依赖冲突(openai 版本),**各用独立 venv**:`eval/.venv`(ragas)/ `eval/.venv-deepeval`。
- 取数通过 HTTP 调后端,不 import backend,与运行时解耦。
- `program` / `empty` 是确定性答案、无检索上下文,两套都剔除并计数。

详见 `eval/README.md`。

---

## 9. 配置与环境

`app/core/config.py` 是 DSN / DATA_DIR / S2_CODES 的**唯一源**,别在模块里重声明。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://postgres:uqrag@localhost:5433/uq_courses` | Postgres DSN |
| `DEEPSEEK_API_KEY` | — | 设了就全程走 DeepSeek |
| `LLM_ENABLED` | `true` | 设 `false` 强制本地 Ollama |
| `OLLAMA_URL` | `http://localhost:11434` | 本地 LLM / embedding |
| `LLM_MODEL` | `qwen2.5-coder:7b` | 本地生成模型 |
| `KB_LLM_GATE` | `1` | P2 可答性 LLM 门开关 |
| `KB_RERANK` | (关) | cross-encoder 重排开关(P1) |

关键阈值常量(改前先看注释里的标定来源):

| 常量 | 值 | 位置 | 含义 |
| --- | --- | --- | --- |
| `SEMANTIC_MIN_SIM` | 0.50 | retrieval | 课程向量召回下限 |
| `kb_search min_sim` | 0.62 | retrieval | KB 召回/拒答阈值 |
| `KB_PREFER_SIM` | 0.55 | qa | 课程语义弱于此且 KB 更强 → 转 KB |
| `KB_STRONG_SIM` | 0.62 | qa | filter 命中空时转 KB 的高门槛 |
| `KB_SOFT_SIM` | 0.60 | qa | KB 软门槛(贴阈交 answerability 裁定) |
| `ANSWER_CAP` / `PROGRAM_CAP` | 20 / 15 | qa | 喂 LLM 的课程/专业上限 |
| `YEAR_LO` / `YEAR_HI` | 2020 / 2028 | answerability | 学年收录区间 |

运行(均从 `backend/`):

```bash
python3 -m pip install -r requirements-dev.txt
uvicorn app.main:app --port 8077        # 或 python -m app.main
pytest                                   # 单测
python -m app.services.qa "CS有哪些课没有考试"   # 直接跑 QA CLI
```

---

## 10. 现状小结

### 已具备的能力

- 6 种 mode 路由 + empty 兜底,LLM 分类 + 大量确定性纠偏,路由抖动有保险。
- 结构化筛选支持:学期/有无考试/hurdle/期中(三态)/小组评估(三态)/学分/校区/授课模式/
  课型/学历层级/学院范围/课码年级/排除课程性质/跨学期合取/「专业 + 筛选」组合查询。
- 课程↔专业关系(c2p / p2c / permit)**确定性枚举,零幻觉**;带方向结构的专业走规则引擎按方向枚举。
- 知识库 FAQ/学术日历/政策问答,跨语言召回 + 三道拒答门(min_sim + P0 + P2)。
- 单课问答:高风险子问题走结构化确定性答案,长尾走 grounded LLM。
- 全链路同步 + SSE 流式两路;每个答案尽量带官方来源 URL。
- 双层评测(确定性断言 + LLM judge),上线前可量化。

### 已知局限 / 待办

- **虚构课程实体**:课程库一侧(非 KB)对虚构实体仍可能不拒答(见记忆 `qa-eval-harnesses`)。
- **bi-encoder 残留噪声**:0.50~0.55 的 off-topic 课归 reranker(P1),目前默认关,靠
  answer 层「相关性诚实」软兜底。
- **难度/通过率无数据**:系统明确不判断课程难度,「低负担」只按客观考核结构排序(红线 1)。
- **数据新鲜度依赖增量任务真的在跑**:`watch_s2` + launchd,学期初需更勤(红线 4)。
- **本机算力天花板**:16GB 本机生成模型上限 7B;重排/更大模型需 DeepSeek 或更强机器。

---

## 附:相关文档与记忆

- 红线:`backend/.claude/rules/student-facing.md`
- 经验:`backend/.claude/rules/lessons-learned.md`、`code-style.md`
- 重排/可答性实测:`docs/rerank_answerability_findings.md`
- 数据进度:`docs/{kb,program,s2,s2_2025,scrape}_progress.md`
- 评测:`eval/README.md`
</content>
</invoke>
