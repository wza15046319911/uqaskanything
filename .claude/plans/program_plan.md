# Program 维度后续计划

> 目标:把已抓的 program 完整规则树用起来 —— 入库、查询、选课模拟器。
> 现状:`program_scraper.py` 完成,**74 个当前 EAIT program** 已抓(`programs.jsonl`),含完整规则树 + plan 递归展开(major / minor / specialisation / field of study)。

## 1. 入库(✅ 已完成:74 programs + 20780 program_course 行,见 `build_programs.py`)

同库(Postgres `uq_courses`,端口 5433)新增两张表:

```sql
CREATE TABLE programs (
    program_id   TEXT PRIMARY KEY,
    title        TEXT,
    total_units  INTEGER,
    rules        JSONB        -- 完整规则树(供选课模拟器)
);

CREATE TABLE program_course (  -- 从规则树派生的扁平关系,供精确查询
    program_id        TEXT,
    course_code       TEXT,    -- 关联 courses.code
    requirement_type  TEXT,    -- core(select_type=all) / elective(select)
    course_list       TEXT,    -- 规则组名,如 "BCompSc Core Courses"
    via_plan          TEXT,    -- 若经由 major/minor 进入,记 plan code(否则 '')
    plan_subtype      TEXT     -- Major / Minor / Specialisation / ...
);
CREATE INDEX idx_pc_course  ON program_course(course_code);
CREATE INDEX idx_pc_program ON program_course(program_id);
```

`build_programs.py`:读 programs.jsonl → 灌 programs(rules 存 JSONB)+ 递归展开规则树派生 program_course。

查询示例:
```sql
-- 某门课是哪些 program 的必修/选修
SELECT p.title, pc.requirement_type, pc.course_list, pc.via_plan
FROM program_course pc JOIN programs p USING (program_id)
WHERE pc.course_code = 'CSSE1001';

-- 某 program 的必修课
SELECT course_code FROM program_course
WHERE program_id = '2559' AND requirement_type = 'core' AND via_plan = '';
```

## 2. 扩到其他学院

- **待解决**:faculty 代码枚举(搜索页 JS 渲染,无可解析下拉;不带 faculty 返回 0)。
- 方案:从 programs-courses 找各学院代码,或用已知 UQ 学院列表逐个 `--faculty <code>` 抓后合并。
- scraper 已支持 `--archived`(默认只当前有效)。

## 3. 选课模拟器(核心目标)

用 `rules` 树驱动:
- 总学分 + 每组 all/select + min/max 学分
- equivalence(等价择一)、wildcard(任意课)、plan 分支(选 major/minor 展开其课)
- 状态机:已选课 → 各规则进度 / 剩余可选 / major 展开(确定性代码,非 LLM)

## 4. 先修课 prerequisite(已暂缓)

- 「选了 A 才解锁 B」需 course prerequisite,是**课程级**数据,不在 program 里。
- 后续:给 `scraper.py` 加 `prerequisite` 字段(与 incompatible 同源),单独过一遍课程页。

## 5. 接入问答

- 「这门课是哪些专业的必修/选修」「某专业要修哪些课」→ 查 program_course。
- 进一步:program 信息进 search_blob 或单独工具,供 LLM 作答。

---

## Roadmap

- [x] program_scraper(完整规则树 + plan 递归展开,支持 `--faculties` 多学院)
- [x] **入库**(programs + program_course:**335 / 66682 行,5 学院** eait/hss/hmbs/bel/sci)
- [x] 扩到其他学院(faculty 代码已确认:eait/hss/hmbs/bel/sci)
- [x] 选课模拟器 `simulator.py`(PlanSimulator:选课/选 major → 进度与可选列表实时变化;units_max 封顶;major 择一;equivalence 去重;自引用排除)
- [ ] 先修课 prerequisite(暂缓)
- [x] 接入问答(qa.py program 模式:课↔专业 必修/选修;如「MATH1061 是哪些专业必修」→ 119 个)

> 注:simulator 的 major 分支按 **select-one(择一)** 默认实现(见 `simulator.py` 文件头),若某 program 的 Major Option 实为可多选需确认后调整。
