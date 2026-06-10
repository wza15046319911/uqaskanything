# 选课模拟器「做到完美」计划

> 目标:把模拟器从「能用」推到「完整正确」——48 学分每一分都能选、能归属、能校验;**程序规则语义对齐官方布尔公式(含 No-Major-Option)**;数据缺口补齐;AI 建议上线;335 个程序不崩;回归测试兜底。
> 前置:`simulator_plan.md` 的阶段一/二/三a/三b/五已全部完成。本计划接着做。

---

## 0. 现状审计(2026-06-10 实测,计划的事实基础)

**数据面(好消息):**
- `courses` 表现在 **3009 行 = 1508 S1 + 1501 S2**,2568 个码,**embedding 0 NULL**(`s2_2025_progress.md`:S2 2025 已全爬入库)。原计划的「S1-only 天花板」大半已塌:2559 的 49 个可选码 **47 个有行**(原来 28)。
- offering:S1/S2 都能从 `courses.semester` 直接拿,`s2_course_codes.txt`(2026:2)继续作 S2 补充。

**数据面(缺口):**
- **S2 那 1501 行先修字段全空**(SQL NULL=未爬,非「无先修」)——S2 爬取用的是加 `prerequisite_raw/parsed` 之前的 scraper,jsonl 里没有该字段,**必须重爬**(ids 清单 `course_ids_s2_2025.txt` 现成,管线两次 0 失败)。2559 的 33 个 no_data 码全因此。

**引擎面(实测确认的 bug):**
| # | 问题 | 实测 |
|---|------|------|
| B1 | **规则满 min 即从可选区消失**:select-type 规则 done 后 `available()/available_by_rule()` 跳过它 | 选 1 门 C.1 课(2u,min=2)→ C.1 的 8 门课全部消失,选不到上限 16。上次只修了 UI 标签,引擎侧没修 |
| B2 | **D 区 19 门课从不可见**:D 有枚举课表(COMP1200/DECO 系列…)但 min=0 初始即 done → 被 B1 同一逻辑隐藏 | `available_by_rule()` 只返回 A/C.1/C.2,无 D |
| B3 | **F 是 wildcard 但引擎不计数**:F 的 items 是 `{kind:"wildcard", org_code:null}` = 任意 UQ 课,但 select 计划外码不计入任何规则 | select PHIL1000 → 接受但 F counted=0,总进度 0。**48 学分填不满** |

**规则语义面(2026-06-10 深挖 AppData,推翻此前「No-Major 只在 HTML 里」的判断):**

官方 AppData 里有**机器可读的程序级布尔公式**,我们的 ingest 把它整个丢了:

```
component header ruleLogic: "Part A AND ( Part B OR Part C ) AND Part D AND Part E AND Part F"
Part C (BCompSc No Major Option): partType=SubRule, unitsMin=8, unitsMax=24,
       ruleLogic="Part C.1 AND Part C.2", children=C.1/C.2
Part E (BCompSc Program Elective Courses): SR4 0–16, body 空
       notes: "any courses on the BCompSc course list" + 选修 L1 ≤14 学分 cap
```

| # | 问题 | 根因(已定位) |
|---|------|--------------|
| R1 | **B 与 C 是 Either/Or,现引擎当 AND**:选 major 的人官方不要求 C.1/C.2,我们强制 C.1≥2 + C.2≥4 | `parse_rules.walk` 丢弃 ruleLogic;C 父节点 items 空被 `if r["items"]` 滤掉,C.1/C.2 被拍平成顶层必修 |
| R2 | **C 的 8–24 总量约束丢失** | C 的 min/max 在 header `unitsMin/unitsMax` 字段(非 selectionRule 的 N/M),scraper 没读 |
| R3 | **E 区(Program Electives 0–16)整个丢失**:语义=「BCompSc 课表内任意课」 | body 空 → `if r["items"]` 滤掉 |
| R4 | **选修 L1 ≤14 cap 未捕获**(独立于程序级 L1 ≤24) | 藏在 E 的 notes HTML 里 |

**结论:先修三个引擎 bug + 把规则语义对齐官方公式(No-Major-Option 随之自然落地),然后才是加功能。**

---

## 1. 成功标准(verify 这些,而不是按步骤打卡)

1. **48/48 可达(两条路径都行)**:2559 从零开始,走 **Major 路径**(A 24 + B 16 + D/E/F 8)或 **No-Major 路径**(A 24 + C 8–24 + D/E/F 补满)都能到 48/48,**每个学分有归属规则**(无静默不计入)。
2. **Major 路径下不强制 C.1/C.2**;**No-Major 路径下 C 的 8–24 总量 + C.1 2–16 + C.2 4–22 同时生效**。
3. **D 区 19 门可见可选可拖**;**E 可搜程序课表内的码**;**F 可搜全库 2568 码**。
4. **min–max 语义全链路一致**:满 min 不收敛、达 max 才收敛(引擎 + UI 同口径);「flatten == available()」不变量随新树形态同步更新后仍成立。
5. **2559 先修 no_data 33 → ≤ 个位数**(S2 重爬回填后,`prereq_report` 验证)。
6. **校验对搜索来的课同样生效**:开课硬拦 / 先修时序 / 学分 cap / 互斥 / L1 ≤24 / 选修 L1 ≤14。
7. **AI 建议**:候选恒 ⊆ 合法池(LLM mock 成乱码也不破),排除数显式披露。
8. **335 程序 sweep 0 crash**,并报告 ruleLogic 解析率 / OR 分组 / SubRule / 空 body part 的全库分布。
9. **pytest 全绿**,断言验证值/结构/错误类型(非「不抛错就过」)。

---

## 2. 阶段与依赖

```
P1 S2 先修重爬(后台长任务,先点火)──────────────────┐
P0 引擎修复 B1/B2 ──► P0.5 规则语义对齐(重爬程序页+公式+   │
                      No-Major+E 区+wildcard 归属 B3)      │
                        └──► P2 课程搜索+E/F 填充 ──► P6 UI 打磨
                        └──► P3 AI 建议 ◄──────────────────┘(吃 P1 覆盖率)
P4 335 程序体检 / P5 回归测试(收尾,依赖 P0–P2 形态冻结)
```

**实施顺序:P1 先点火(后台)→ P0 → P0.5(程序页重爬也是后台,与 P1 错峰不并发打 UQ)→ P2 → P6 → P3 → P4 → P5。**

---

## P0 引擎修复(B1/B2,最优先)

**B1+B2(同根):** `available()` / `available_by_rule()` 的收敛条件从「rule done(min 满足)」改成「**select-type 规则达 units_max 才收敛**」(无 max 的 all 型规则维持原语义)。D(0–16)/C.1(2–16)/C.2(4–22)随之持续供选到 max。
- 陷阱:`available_by_rule` 的「flatten == available()」验收不变量要随新口径**同步改两边**,不能只改一边。
- 陷阱:units_max 封顶计数(over_max)语义不变——能继续**选**不等于多算学分。

**验收:** 选 1 门 C.1 课后 C.1 仍供选(到 16u 才收敛);D 区 19 门出现在 `available_by_rule`;原 CLI 自测 + 既有端到端断言全过(口径更新处同步改断言)。

## P0.5 规则语义对齐(No-Major-Option + ruleLogic + E 区 + wildcard 归属)

> 用户拍板:No-Major-Option **做**。深挖后发现它不是孤立功能,而是「ingest 丢了官方布尔公式」这个根因的一个症状——一起修。

**① scraper(`program_scraper.py`):**
- 捕获 component header 的 `ruleLogic` → 程序级 `rule_logic` 字符串。
- SubRule 父节点(如 C)**不再被 `if r["items"]` 滤掉**:输出 `{ref, title, part_type:"SubRule", units_min/units_max(读 header.unitsMin/unitsMax), rule_logic, children_refs:[...]}`(树保持扁平,父子用 ref 关联——对 status()/UI 的改动最小;见决策 #6)。
- 空 body 但有 selectionRule 的 part(如 E):输出 wildcard-program-list 规则 `{ref:"E", items:[{kind:"wildcard_program_list"}], units_min:0, units_max:16}`;同时抓 notes 文本,正则 `no more than (\d+) units at level (\d+)` 捕获**选修 L1 cap** 进 aux 类数据。
- **重爬 335 个程序页**(管线现成,跑过一次全量;与 P1 错峰)。

**② ingest(`build_programs.py`):** programs 表加 `rule_logic TEXT` 列(幂等 ALTER);新树形态入库。

**③ 引擎(`simulator.py`):**
- `_parse_rule_logic(s)`:微型递归下降(token:`Part REF` / AND / OR / 括号)→ 布尔树。**缺失或解析失败 → 回退 AND-all(现语义)并在 status 透出 `logic_fallback: true`**(rule 19,不静默)。
- SubRule 父规则:counted = Σ children counted,自身 min/max(C:8–24)独立判定;done = 公式 `Part C.1 AND Part C.2` ∧ 自身 min 满足。
- **OR 分支选择**:SimState 加 `branch: dict`(如 `{"B|C": "B"}`),客户端显式选;**默认 "B"(Major 路径)**。非活跃分支的规则:不计入必修需求、其课程可被归属到 E/F(见 ④);`status()` 透出 `branch_groups:[["B","C"]]` + 当前分支,UI 据此渲染。
- 程序整体满足 = 布尔公式求值(叶子 = 该 part done)。
- **选修 L1 ≤14 cap**:并入 `level_cap_status()` 同形态输出(scope 标 "electives"),计数口径=非 A/B 归属的已选课。

**④ wildcard 归属(原 B3,移到这里——因为它依赖 E):**
引擎把「活跃规则枚举外的已选码」按**确定性优先序**归属:
1. 码在 D 的枚举表内且 D 未达 max → D;
2. 否则码在**程序课表**内(树内全部枚举码之并)且 E 未达 max → E;
3. 否则 F(任意码,需存在于 `courses` 表)未达 max → F;
4. 都满 → 仍接受 select,`status()` 新增 `unattributed: [code,...]` 显式透出。
- `program_exclude`(MATH1040)优先排除;非活跃分支(如 Major 路径下的 C.1/C.2 课)按此序流入 E/F。
- **实现前先读现有归属语义**(`_walk_units` 计数路径),扩展而不是另写一套(rule 15)。

**⑤ UI(`web/simulator.html`):**
- B/C 渲染成「路径二选一」容器:顶部 toggle **「修 Major / No Major」**(默认 Major),切换时另一分支折叠灰显、其课程自动流转归属;localStorage 记 branch。
- C 父规则头显示 `8–24` 总量进度(子规则 C.1/C.2 照常各自显示)。
- 选修 L1 cap chip:`选修L1 X/14` 与 `L1 X/24` 并排,超标红。

**验收:** Major 路径:A 满 + B 选 major + 不碰 C.1/C.2 → 公式可满足、总进度能到 48;No-Major 路径:toggle 切换后 C 显示 8–24、C.1/C.2 必修生效、B 折叠;select PHIL1000 → F counted=2.0;select 一门 BCompSc 课表内的计划外课 → 归 E;E 满 16 后流 F;全满后进 `unattributed`;`rule_logic` 缺失的程序回退 AND-all 且 `logic_fallback=true`;选修 L1 15u → 红标。

## P1 S2 先修重爬(后台,先点火)

- 用**现版** `scraper.py`(带 prereq 解析)对 `course_ids_s2_2025.txt` 的 1501 个 offering 重爬 → 新 jsonl → 经 `build_db.py` 路径回填 `prerequisite_raw/parsed`(只 UPDATE 先修两列,不动 embedding)。
- 后台任务规则:`run_in_background=true` 且**不带内层 `&`**(前车之鉴:双层后台会被收割)。
- 失败码显式列出(0 失败为目标,>0 则逐码报告);完成后跑 `prereq_report` 全库 + 2559 复核。
- **验收:** S2 行 `parsed` 三态计数合理(真树/JSON null/raw);2559 `no_data_not_scraped` 33 → ≤ 个位数;S2 课(如 COMP3506)locked/unlocked 行为正确。

## P2 课程搜索 + E/F 区填充(原「(a)」)

- **后端:** `GET /api/sim/courses?q=&in_program=` —— code/title ILIKE 搜索,`DISTINCT ON (code)`,LIMIT 50,返回 hydrate 同形态卡片元数据 + offerings;`in_program=2559` 时限定程序课表内的码(E 区用),不带则全库(F 区用)。
- **前端:** E/F 区各一个搜索框(共用组件);结果卡可点选/可拖入时间表;已计入 E/F 的课分区列出(带 ×);D 区经 P0 后自动以普通规则组渲染。
- **校验链:** 搜索来的课走同一条 `placement` 校验(`/api/sim/state` 的 codes 集合并入计划外已选码,offerings/locks/hydrate 确认覆盖)。
- **验收:** 搜 "philosophy" 出 PHIL 课;拖 PHIL1000 进 Y1S1 → F 1 门 2u、总进度 +2;E 区搜索只出程序课表内的码;S2-only 搜索课拖 S1 格被硬拦;先修未满软红标;F 16u 满后再加给 unattributed 警示。

## P3 AI 建议(阶段四,sim_advise.py)

原计划 §阶段四 设计不变(确定性定池、LLM 只排序、guard_citations 双护栏、temperature=0),两处因数据/语义变化更新:
- **可达池披露更新:** 现在 47/49 有 embedding(原 28/49),`excluded_count` 按实时查询算,不写死。
- **候选口径(P0.5 之后):** available 不再是封闭集合(E/F=开放)。定义:候选 = `semantic_search(goal, k=40)` 全库召回 ∩(活跃分支枚举内未选 ∪ E/F 可计入且未选),每个候选标注**归属规则 ref**(「这门课会计入 E」),排除 `program_exclude` 与已选。
- 端点 `POST /api/sim/advise {program_id, state, goal}`;UI 给一个 goal 输入框 + 建议卡(点卡即选)。
- **验收:** 空池短路 0 次 LLM;LLM mock 返编造码 ZZZZ9999 → 被剥除进 dropped;候选全部 ⊆ 合法池且带归属 ref;中文 goal(「我想做 AI 安全」)直喂 bge-m3 出合理候选。

## P4 335 程序体检(广度健壮性)

- sweep 脚本:对全部 335 个 program_id 建 `PlanSimulator`,跑 `status()/available_by_rule()/level_cap_status()/units_map()`,断言不崩 + flatten 不变量;归类报告:**ruleLogic 解析率 / OR 分组数 / SubRule 数 / 空 body part 数** / 多 major / 深嵌套 / aux_rules 类型分布。
- **只修 crash 类**;形态怪但不崩的列清单待定,不展开做(防 scope 爆炸)。
- **验收:** 0 crash;报告产出(异常清单 + 计数)。

## P5 回归测试套件(pytest)

- `test_simulator.py`:min–max 收敛 / 公式求值(AND/OR/括号/回退) / 分支切换 / SubRule 8–24 / wildcard 归属优先序 D→E→F / unattributed / equiv 收敛 / exclude / 双 L1 cap 值断言。
- `test_scheduler.py`:拓扑序 / cap 不超 / offering 钉死 / 环→unplaced / 零静默丢恒等式。
- `test_parse_prereq.py`:AND/OR 树 / 缩写展开(`1111→ACCT1111`)/ raw 兜底 / 空→null。
- API 冒烟:404/400/搜索/advise 护栏。
- 全部断言**值与结构**(rule 16),不写「不抛错就过」的烟雾测试。

## P6 UI 打磨(跟随 P0/P0.5/P2 落地)

- D 区规则组渲染核查(0-min 规则的进度条/标签,沿用「可选 0–16」口径)。
- E/F 区:搜索框 + 已计入列表 + `E X/16`、`F X/16` 进度。
- Major/No-Major toggle 的折叠/流转动效与状态恢复。
- `unattributed` 黄色警示条(「N 门课未计入任何规则」)。
- 总进度条计入 D/E/F 学分(随归属逻辑自动,验证)。
- 服务端 500/断网的错误提示(现状确认,缺则补 toast)。

---

## 3. 决策点(建议已给,可推翻)

| # | 决策 | 结论 | 理由 |
|---|------|------|------|
| 1 | wildcard 归属优先序 | **D 枚举 → E(程序课表)→ F(任意)→ unattributed** | 确定性、稀缺优先 |
| 2 | AI 建议是否进「完美」范围 | **进**(P3) | 原定 optional,但「完美」应含;数据面已就绪 |
| 3 | No-Major-Option | **做(用户拍板)**——并入 P0.5,按官方 ruleLogic 结构化实现,非解析 HTML 摘要 | 深挖发现 `ruleLogic`/SubRule/unitsMin-Max 全是结构化字段,此前「脆弱」判断不成立 |
| 4 | React 迁移 | **不进本计划** | 纯重写无新功能;等功能冻结后再议 |
| 5 | S2 重爬范围 | **只回填先修两列** | embedding/行数据已是好的,不动 |
| 6 | 新树形态 | **扁平 + SubRule 父节点带 `children_refs`**(不深嵌套) | status()/UI 按顶层 ref 渲染的现有代码改动最小;深嵌套是更大重构,收益为零 |
| 7 | OR 分支交互 | **显式 toggle,默认 Major(B)**,存 SimState.branch | 从已选推断分支会有歧义(两边都沾时);显式最确定 |

## 4. 预算(rule 13)

- 每个 bug 修复:**最多 3 轮**调试循环,超则停下来报告现状。
- P1 课程重爬:**1 次**全量(1501 页)+ 失败码补爬 **1 次**;再失败则列码待定。
- P0.5 程序重爬:**1 次**全量(335 页,与 P1 错峰不并发);失败程序逐个列出。
- P4 sweep:1 次 + 修复后复跑 1 次。
- 全计划单 session 内分阶段交付,每阶段完成 → 跑该阶段验收 → 记 checkpoint 进本文件 Roadmap。

## 5. Roadmap(2026-06-10 全部完成)

- [x] **P1 S2 先修重爬**:1501/1501 爬成 0 失败 → 按 offering_id 回填(0 未匹配)→ S2 行三态 655 真树/215 raw/631 确无;2559 no_data **33→5**(剩 5 门两学期都不在 St Lucia/In Person 开,真实缺口:COMP1200/COMP2200/CSSE3610/DECO2840/STAT3007)。COMP3506 的 OR 先修树实测正确。
- [x] **P0 引擎修复**:`_closed()`——有 max 的组到顶才收敛;D 区 19 门可见;flatten==available() 不变量两侧同步,旧断言全过。
- [x] **P0.5 规则语义对齐**:scraper 抓 ruleLogic/SubRule(header.unitsMin/Max)/空表 part/notes;**335 程序重爬 0 失败入库(334 有公式、60 含 OR 分支、200 含 SubRule——远不止 2559 用得上)**;引擎 `parse_rule_logic`(大小写容错+前导连接词容错)+ `choose_branch`/`branch_state`(默认组内第一个=Major)+ C 父规则 8–24(子先封顶再求和)+ 归属 D枚举→E(程序课表)→F(任意,undergraduate notes 限 level≤6)→unattributed + **claims 树序先到先得防一码双计(修掉 DECO2801 双计的预存在漏洞)** + 选修 L1≤14(notes 数据驱动);UI 二选一 toggle(切换流转归属、localStorage 持久化)。两条路径都实测 48/48 + 公式满足。
- [x] **P2 课程搜索 + E/F 填充**:`GET /api/sim/courses?q=&in_program=`(ILIKE+DISTINCT ON+LIMIT 50+offerings);E/F 区搜索框(防抖 250ms,结果 offerings 并入拖拽硬拦);校验链对树外课全覆盖(`_validate`/schedule 学分查全库兜底)。
- [x] **P3 AI 建议**:sim_advise.py(确定性定池=枚举可选∪E/F 可计入含 level 约束;候选标注归属 ref;召回<3 降地板重试;guard_citations 剥越界码;unreachable 显式披露)+ `POST /api/sim/advise` + UI goal 输入(建议卡点选即入计划,Playwright 闭环验证)。
- [x] **P4 335 程序体检**:335/335 OK、0 crash、flatten 不变量全成立;fallback 仅 2534(官方公式引用了被丢的空容器 B,AND-all 诚实降级)。
- [x] **P5 回归测试**:`test_simulator.py`(18)+ `test_scheduler.py`(7)+ `test_parse_prereq.py`(8)+ `test_server.py`(8,含 LLM mock 编造码 ZZZZ9999 被剥除断言)= **41 passed**。
- [x] **P6 UI 复验**:先修锁标 28 个、S2-only 点选自动落 S2 学期、先修时序软红标、分支/落位刷新持久化、0 JS 错误。

## 6. 修订(用户反馈:样式崩塌)

用户报 /sim 界面崩塌(左栏被裁出屏外、右栏被下拉浮层压住)。多 agent 视觉扫描(7 视口/状态)定位 6 处缺陷,一次性修(均在 `web/simulator.html` CSS/JS):
- [x] **右栏时间表与左栏高度失衡(主因)**:双栏 grid 把右栏白卡拉到与左栏等高,留几千 px 空白。修:`.layout{align-items:start}` + 右栏 `position:sticky`(课表很长时钉在视口内自滚)。右栏高度 2794→578,滚动后 top 留 16。
- [x] **二选一(equiv)卡药丸溢出被裁**:长课名内层 flex 行缺 `min-width:0`,标题不收缩把"先修待核/S2"药丸顶出卡外。修:两层内嵌 flex 行加 `min-width:0`,药丸 0 溢出、课名省略号。
- [x] **.ttgrid 单元格右溢出面板 ~17px**:列 `1fr→minmax(0,1fr)` + `.ttcell/.pane min-width:0`。
- [x] **分支 toggle 换行挤压**:`.brtoggle{flex-wrap}` + label/药丸 `nowrap`。
- [x] **统计短语断词**("达/下限"、"4–/22"):`.runits{white-space:nowrap}`。
- [x] **hero 副标题孤行**:`.sub{text-wrap:balance}`。

验收:7 视口/状态 **0 水平溢出**;3 agent 对抗复验逐条确认修复、**0 回归 0 新问题**。
已知未修(非样式、记录待定):① 860 单栏下时间表在底部、拖课看不到落点(可点卡自动放,可用性未断);② STAT3007 等 5 个 no_data 码显示"(无开课信息)"却挂 S2 药丸——offerings(2026:2 清单)与 hydrate(St Lucia/In Person profile)口径不同的边界,仅影响 5 码显示。

> 关联:`simulator_plan.md`(MVP+增强,已完成部分)/ `s2_2025_progress.md`(S2 数据来源)/ `s2_progress.md`(2026 S2 码清单)。
