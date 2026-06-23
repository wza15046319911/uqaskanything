# 课程攻略入库进度

日期:2026-06-21
来源:`backend/data/original_guides/` 下手写攻略 docx(学长经验贴,匿名)
依赖:已建好的攻略管线(`guide_check` 对账闸门 + `guide_build` 双库入库),设计见 [course-guide-ingestion.md](course-guide-ingestion.md)。

## 当前已入库课程(本地 :5433 + Supabase,共 12 门 39 块)

| 课程 | 官方标题 | year | semester | 块数 | 批次 |
|---|---|---|---|---|---|
| COMP4500 | Advanced Algorithms & Data Structures | 2025 | S2 | 3 | 第一批(随设计) |
| COMP7500 | Advanced Algorithms & Data Structures | 2025 | S2 | 3 | 第一批(随设计) |
| INFS7410 | Information Retrieval and Web Search | 2025 | S2 | 3 | 第一批(随设计) |
| DATA7201 | Data Analytics at Scale | 2026 | S1 | 3 | 第二批 |
| DATA7202 | Statistical Methods for Data Science | 2026 | S1 | 3 | 第二批 |
| DATA7901 | Data Science Capstone Project 1 | 2025 | S2 | 3 | 第二批 |
| DATA7903 | Data Science Capstone Project 2B | 2026 | S1 | 3 | 第二批 |
| INFS7901 | Database Principles | 2026 | S1 | 3 | 第二批 |
| COMP4703 | Natural Language Processing | 2025 | S2 | 4 | 第三批 |
| CSSE6400 | Software Architecture | 2026 | S1 | 3 | 第三批 |
| INFS3208 | Cloud Computing | 2025 | S2 | 4 | 第三批 |
| DECO7381 | Design Computing Studio 3 - Build | 2025 | S2 | 5 | 第三批 |

攻略文件:`backend/data/guides/<CODE>_<YEAR>.md`(DATA7901 + 第一批 3 门是 `_2025`,DATA7201/7202/7903/INFS7901 是 `_2026`)。

- **第一批(COMP4500 / COMP7500 / INFS7410)**:跟攻略管线设计一起入库,作为对账闸门的验证样本——
  COMP4500/7500 当初故意拿「把两门写成一门」的错样本测闸门(先修 COMP3506 vs COMP7505、作业权重
  20+20 vs 15+15、期末 50 vs 60、都漏标期末 Hurdle),拆成两篇并修正后才过;INFS7410 是事实层近满分的正样本。
- **第二批**:`original_guides/` 下 5 个 DATA/INFS docx。
- **第三批(本次)**:4 门来自学生投稿文本(COMP4703 / CSSE6400 / INFS3208 / DECO7381),
  guide_check 4/4 全过 0 冲突,双库各 +15 块,`checked_at=2026-06-21`。关键判断:
  - **投稿开头那段「口试/RAG/信息检索」碎片属于 INFS7410**(已入库),非 COMP4703——
    COMP4703 是笔试 Final Exam,不是口试,没误并进去。
  - **CSSE6400 考核结构以官方为准**:学生说 A1 10%+A2 10%+A3 20%+组 40%,但官方是
    Cloud Infrastructure 40 + Architecture Presentation 30(hurdle)+ Delivering Quality
    Attributes 30(hurdle);claims 按官方三项,学生的 A1/A2/A3 子拆分写进正文并标「以当年 ECP 为准」。
    经验对应 **2026 S1**(用户补正;文件 `CSSE6400_2026.md`,已删两库旧 year=2025 块各 3 块后重灌)。
  - **「REIT 选导师攻略」(REIT7841/4841/DATA7901)暂跳过**:是跨课程的 thesis portal 选项目/
    发邮件流程经验,不绑单课考核事实,不适配「一课一篇 + claims 对账」模型,等有合适载体再处理。

## 处理流程(已完成)

1. **抽文本**:docx 用 zipfile + XML 解析(不装 python-docx,不污染环境)。
2. **校准 claims**:逐门拉 `course_detail`,frontmatter.claims 的 prereq / 各考核权重 / hurdle
   严格按官方写(闸门按值比对,不一致会被拦);学长笔记里的主观经验进正文 `##` 小节。
3. **对账闸门**:`guide_check` 5 篇全过、0 冲突。
4. **双库入库**:`guide_build` 本地 :5433 + Supabase 各 8 篇 24 块(含旧 3 门),`checked_at=2026-06-21`。
5. **year/semester 修正**(用户补充开课口径后):DATA7201/7202/INFS7901/DATA7903 由 2025 改 2026 并补 S1,
   DATA7901 补 S2;文件名同步重命名;清掉两库旧 `year=2025` 残留块(各 12 块)后重灌;查库确认两库一致。

## 关键判断(留档)

- **「40% hurdle」是及格线不是权重**:学长说的「Exam 40% Hurdle」指该考核 hurdle 通过线,不是占比。
  claims 的 `weight` 一律按官方占比,`hurdle` 只标布尔;及格线作为经验写进正文(标「以当年 ECP 为准」)。
- **DATA7901 的 Proposal hurdle 冲突**:学长称 Proposal(60%)也卡 hurdle,但官方只有 Presentation(30%)标
  hurdle。claims 跟官方(否则闸门拦),正文软提「据当届经验 Proposal 也可能卡 Hurdle,以当年 ECP 为准」,
  不当事实。
- **两个 7901 要区分**:DATA7901(capstone)= 2025 S2,先于 DATA7903(2026 S1);INFS7901(数据库)= 2026 S1。
- **DATA7903 官方标题是 "Capstone Project 2B"**:学长写的是 7901 的实践篇,先修 / 权重 / 双 hurdle 与官方一致。

## 待办 / 风险

- ⚠ **轮换 Supabase DB 密码**:拼云端 DSN 时一次 `urlsplit` 校验报错把密码片段打进了回显,已泄进会话日志。
  后续命令已改为只走子进程 env + 过滤回显,但已暴露的串建议轮换。
- 本批未跑攻略 eval(管线无独立攻略 eval);如要可补一组 guide-mode 真题人工核对。
