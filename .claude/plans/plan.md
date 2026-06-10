# UQ 课程知识库问答系统 — 项目计划

> 目标:抓取 UQ course-profiles,构建覆盖所有课程的知识库,提供自然语言问答入口。
> 例如「哪些课程 S2 开放」「哪些课程没有考试」「找跟机器学习相关的课」,检索知识库并返回对应课程。

---

## 1. 总体架构

系统分四层,数据从上往下流动:

```
┌─────────────────────────────────────────────────────────┐
│  采集层 Scraper        UQ course-profiles → 结构化 JSON   │  ← 已完成
├─────────────────────────────────────────────────────────┤
│  存储层 Knowledge Base  SQLite/PostgreSQL + 向量库(pgvector)│
├─────────────────────────────────────────────────────────┤
│  查询层 Query Engine    LLM-to-SQL(精确) + 向量检索(语义)  │
├─────────────────────────────────────────────────────────┤
│  问答入口 Q&A Entry     API / 前端,自然语言提问           │
└─────────────────────────────────────────────────────────┘
```

设计上的关键判断:**用户的问题分两类,走两条不同的检索路径**,不要混为一谈。

| 问题类型 | 例子 | 检索方式 | 是否需要 LLM/向量库 |
|---------|------|---------|-------------------|
| 精确过滤 | 哪些课程 S2 开放 / 没有考试 | 结构化字段 SQL 过滤 | LLM 转 SQL,不碰向量库 |
| 语义模糊 | 找跟机器学习相关的课 | 向量相似度检索 | 向量库 embedding |
| 混合 | S2 开放的、跟 AI 相关的课 | SQL 过滤 + 向量重排 | 两者结合 |

这就是为什么 schema 要把字段分成三类(见下)。

---

## 2. 数据 Schema 设计

每门课解析成一条 JSON 记录,字段分三类,分别服务于不同检索路径:

### 2.1 结构化字段(给 SQL / LLM-to-SQL)

| 字段 | 类型 | 说明 | 典型查询 |
|------|------|------|---------|
| `code` | str | 课程码 CSSE1001 | — |
| `offering_id` | str | URL slug,主键 | — |
| `title` | str | 课程名 | — |
| `study_period` | str | 原始开课周期文本 | — |
| `semester` | str | **派生**:S1/S2/Summer | 哪些课 S2 开放 |
| `year` | int | **派生**:年份 | 2026 年开的课 |
| `location` | str | St Lucia 等 | 哪些课在 St Lucia |
| `attendance_mode` | str | In Person/External | 哪些课可以远程 |
| `level` | str | Undergraduate/Postgraduate | 研究生课程 |
| `units` | float | 学分 | — |
| `coordinating_unit` | str | 开课学院 | — |
| `coordinator` | str | 课程协调人 | — |
| `incompatible` | list | 互斥课程码 | 跟 X 冲突的课 |
| `has_exam` | bool | **派生**:是否含考试 | 哪些课没有考试 |
| `has_hurdle` | bool | **派生**:是否含 hurdle | 有 hurdle 要求的课 |

### 2.2 评估明细

```
assessments: [
  { task, category, weight, hurdle }   # 每项评估的名称/类别/权重/是否hurdle
]
```

### 2.3 文本字段(给向量库做 embedding)

| 字段 | 说明 |
|------|------|
| `description` | 课程简介 |
| `learning_outcomes` | 学习成果列表 |
| `topics` | 周次 lecture 主题摘要 |
| `learning_activities` | **无损保留**全部活动:period/activity_type/topic/关联LO |
| `search_blob` | 聚合上述文本,直接拿去算 embedding |

**派生字段是设计核心**:原页面只有「Semester 1, 2026」和一张评估表,scraper 把它们规范化成 `semester=S1`、`has_exam=true` 这种可直接过滤的值。这样 LLM 写 SQL 时面对干净字段,不用每次解析自由文本。

---

## 3. 采集层(已完成)

### 3.1 状态

已实现并实测通过(CSSE1001 全字段正确解析):

- 单个 / `--file` 批量抓取
- 输出 JSONL,每行一门课
- 派生字段 `semester` / `has_exam` / `has_hurdle` 自动计算
- `learning_activities` 无损保留全部活动 + 关联 LO
- `search_blob` 为向量库预留

### 3.2 用法

```bash
# 单个
python uq_scraper.py CSSE1001-21206-7620

# 批量(course_ids.txt 每行一个 offering id)
python uq_scraper.py --file course_ids.txt --out courses.jsonl --delay 1
```

### 3.3 已知限制

- **依赖 offering id 清单**:UQ 无公开全量课程 API,需先从 programs-courses 爬课程码再解析 offering id(已由 `collect_ids.py` 解决,见第 7 节)。
- 解析基于当前页面 HTML 结构,UQ 改版可能需调整正则。

---

## 4. 存储层(待做)

### 4.1 SQLite/PostgreSQL 建表

结构化字段建表,`assessments` / `learning_activities` 用 JSON 列或子表。规模判断:

- 几百~上千门课:SQLite 够用
- 需并发/语义检索:PostgreSQL + pgvector(结构化与向量同库,省一套依赖)

建议表结构:

```sql
CREATE TABLE courses (
    offering_id   TEXT PRIMARY KEY,
    code          TEXT,
    title         TEXT,
    semester      TEXT,        -- S1/S2/Summer,索引
    year          INTEGER,
    location      TEXT,
    attendance_mode TEXT,
    level         TEXT,
    units         REAL,
    coordinating_unit TEXT,
    coordinator   TEXT,
    has_exam      BOOLEAN,     -- 索引
    has_hurdle    BOOLEAN,
    incompatible  JSONB,
    assessments   JSONB,
    learning_activities JSONB,
    description   TEXT,
    search_blob   TEXT,
    embedding     VECTOR(1536) -- pgvector,给语义检索
);
CREATE INDEX idx_semester ON courses(semester);
CREATE INDEX idx_has_exam ON courses(has_exam);
```

### 4.2 灌库脚本

读 JSONL → 写入表 → 对 `search_blob` 算 embedding 写入 `embedding` 列。

---

## 5. 查询层(待做)

### 5.1 LLM-to-SQL(精确过滤)

把 schema 说明 + 用户问题给 LLM,生成 SQL 并执行:

```
用户:哪些课程 S2 开放
  → SELECT code, title FROM courses WHERE semester = 'S2'

用户:哪些课程没有考试
  → SELECT code, title FROM courses WHERE has_exam = false
```

要点:
- 在 prompt 里给 LLM 完整字段说明 + 枚举值(semester 只有 S1/S2/Summer)
- 限制只能生成 SELECT,防注入
- 复杂问题可让 LLM 先判断走 SQL 还是向量检索

### 5.2 向量检索(语义模糊)

「找跟机器学习相关的课」→ 对问题算 embedding → 在 `embedding` 列做相似度检索 → 返回 top-k。

### 5.3 混合查询

「S2 开放的、跟 AI 相关的课」→ 先 SQL 过滤 `semester='S2'`,再在结果内做向量重排。

---

## 6. 扩展:program(培养方案)维度

> 目标:支持「某门课是哪个 program 的必修/选修」「某 program 的必修课有哪些」这类查询。

**关键判断:课程 schema 不动。**「必修/选修」不是课程的属性,而是 (program, course) 这对关系的属性——同一门 CSSE1001 可能是 A 专业必修、B 专业选修。所以它独立成一张多对多关系表,不塞进 course 记录。

### 6.1 新增两张表

```sql
CREATE TABLE programs (              -- 专业维度
    program_id   TEXT PRIMARY KEY,   -- UQ program 码 / slug
    title        TEXT,               -- Bachelor of Computer Science
    level        TEXT                 -- UG / PG
);

CREATE TABLE program_course (        -- 关系表(核心)
    program_id        TEXT,          -- -> programs.program_id
    course_code       TEXT,          -- -> courses.code(注意:不是 offering_id)
    requirement_type  TEXT,          -- 'core' 必修 / 'elective' 选修 / 'option'
    course_list       TEXT,          -- 所属规则组,如 "Part A Compulsory" / "AI Major"
    PRIMARY KEY (program_id, course_code, course_list)
);
```

### 6.2 查询示例

```sql
-- 某门课是哪些 program 的必修/选修
SELECT p.title, pc.requirement_type
FROM program_course pc JOIN programs p USING (program_id)
WHERE pc.course_code = 'CSSE1001';

-- 反向:某 program 的必修课
SELECT course_code FROM program_course
WHERE program_id = ? AND requirement_type = 'core';
```

### 6.3 关键 join key:按 code 关联,不是 offering_id

UQ 培养方案引用课程用 **course code**(CSSE1001),不是 offering id(CSSE1001-21206-7620)。所以关系表按 `code` 关联。当前每门课只抓 1 个 offering(St Lucia / S1),`code` 在 courses 表里基本唯一,join 干净;即使以后一门课抓多个 offering,program 关系本就与具体开课无关,永远挂在 `code` 上,设计不破。

### 6.4 方案对比(为什么用独立关系表)

| | A. 独立关系表(选用) | B. course 里塞 `programs:[{program,type}]` |
|---|---|---|
| 正向查(课 → program) | ✅ | ✅ |
| **反向查(program → 必修课)** | ✅ 一条 SQL | ❌ 全表扫 JSON |
| 关系属性(规则组 / 选 N 学分) | ✅ 自然 | ⚠️ 塞不下 |
| 契合 LLM-to-SQL | ✅ 标准 join | ❌ |

B 仅在「永远只正向查」时省事;本项目需双向,故选 A。

### 6.5 数据来源

programs-courses 的 **program 页面**(列出 compulsory / elective course list)。独立爬虫 `program_scraper.py`,与 course 数据解耦,属后续阶段(见 Roadmap 阶段六)。

---

## 7. offering id 清单来源(已解决)

✅ 已由 `collect_ids.py` 解决:programs-courses 搜索页(按学期过滤)拿到全部课程码 → 逐门 `course.html` 解析开课表,按 学期 + 校区 + 模式 过滤取 course-profiles 的 offering id。已产出 `course_ids.txt`:**1508 门** S1 2026 / St Lucia / In Person。

```bash
python collect_ids.py --semester 2026:1 --location "St Lucia" --mode "In Person" --out course_ids.txt
```

原备选方案(留作记录):

1. 从 [programs-courses.uq.edu.au](https://programs-courses.uq.edu.au) 爬课程码 → course.html 解析 offering id  ← 采用此路
2. 从 [UQ public timetable](https://timetable.my.uq.edu.au) 取某学期所有开课
3. 手工维护关注的课程清单(小范围起步)

---

## 8. 实施 Roadmap

- [x] **阶段一**:scraper + schema 设计(已完成)
- [x] **阶段二**:offering id 清单采集(`collect_ids.py`,已产出 `course_ids.txt`:1508 门 S1 2026 / St Lucia / In Person)
- [x] **阶段三**:存储层(`build_db.py` + `embed.py`,Postgres+pgvector,1508 门全 embedding)
- [x] **阶段四**:查询层(`qa.py`:planner 路由 + retrieval RRF 混合检索 + answer 生成,本地 qwen + bge-m3)
- [~] **阶段五**:问答入口(`qa.py` CLI 已可用 + `eval.py` 评测;API/前端未做)
- [x] **阶段六**:program 维度(`program_scraper.py` + `build_programs.py`:335 program / 5 学院;`simulator.py` 选课模拟器;qa.py 接入)

> 详细后续计划见 [.claude/plans/qa_accuracy_plan.md](qa_accuracy_plan.md) 与 [.claude/plans/program_plan.md](program_plan.md)。