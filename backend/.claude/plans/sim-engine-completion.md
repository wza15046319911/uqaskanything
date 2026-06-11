# 选课模拟器引擎 — 收尾计划(冲刺"完美无瑕")

## 目标
68 个 EAIT 程序在模拟器里都能被真实学生排满到 `formula_satisfied=True`;凡排不满的,
必须能明确解释成"真实学制约束"而非引擎/数据缺陷。

## 当前状态(2026-06,分支 `fix/sim-rule-engine`)
- 已修 **7 个引擎 bug**(见 commit dc56486 + afe8f1c,详见 memory `sim-rule-engine.md`):
  嵌套上卷、OR-done branchable、共享课增广匹配、畸形 rule_logic 健壮性、负 semIndex、
  augment 非均匀学分腾挪、from-plans 选修错继承 plan min。
- 广测 68 个 EAIT 程序:**0 崩溃 / 0 静默丢课 / pytest 42 过**;**49/68 可排满**(原 42)。
- 剩余 **19 个**排不满,根因已分类(见下)。基本是数据层长尾,不是引擎。

## 验证基线(每步必做)
- `cd backend && PYTHONPATH=. python3 -m pytest -q`(须保持 42 过)。
- `python3 /tmp/uqsim/batch.py`(完成率;每步不得回退)。
- 抽改动相关程序,对官方 `programs-courses.uq.edu.au`(数据内联在 `window.AppData`)逐规则核对。
- 工具:`/tmp/uqsim/{sim.py 调 API, batch.py 完成率, maxc.py 激进全选, official.py 官方逐规则/plan 池}`。

---

## 剩余 19 程序 · 按优先级的修复计划

### P1 — 引擎可控,高杠杆

**任务 1:展开 Program 型跨程序引用**(2560 B.3、5257 A.4 自引用、2557 A.6)
- 现状:规则 body 是 `CurriculumReference → 整个 program`(数字 code,如 5257,subtype=Postgraduate Program),
  引擎只展开 Course / Plan 型,不展开 Program 型 → 该规则池永远空(0/2)。且 5257 的 12 门选修课
  (CSSE7030/7231、INFS7901、LAWS7023、MATH7051/7307/7308/7861、MGTS7303/7601/7619、STAT7203)
  **根本不在 `courses` 表**。
- 三步(缺一不可):
  1. **引擎**:`_claimable_codes / _collect_codes / _claims / _base_entry` 把"plan item 且 `code.isdigit()`
     (Program 引用,非 `_is_self_program`)"当**自动展开池**(无需 chosen_plans),不列为 UI 可选 major。
  2. **scraper**:`program_scraper.py:fetch_plan_rules` 对数字 code 用 `program_list.html?acad_prog=` 端点
     (现统一用 `plan_display.html?acad_plan=`,对 program 引用抓错)。
  3. **数据**:补抓 5257 等被引用 program 的选修课入 `courses` 表 → 重爬 2560/2557/5257,重建这几行。
- 验收:2560/2557/5257 可排满。

**任务 2:2460 数学单 A.2.1=0/16**
- 现状:所选 major 的 16u 被无 units_max 上限的 A.3.1 吸光(18–26u),A.2.1 永远 0。
- 修:给无上限的选修规则(如 A.3.1)在 attribution 时**补隐式 units_max 帽子**(= 其在父规则下的剩余配额),
  或让 `_claims` 匹配时优先喂"更专属/更紧"的规则(核心/major 优先于通用 elective)。
- 注意:这是已修的"增广匹配优先级"的延伸,改动须回归全部 CS/数学程序。
- 验收:2460 可排满;CS/数学无回归。

### P2 — 数据对账(逐程序 + 可能重爬)

**任务 3:工程 discipline major `A.2.1=34/36`(~12 程序)**
2485/2487/2488/2569/2556/2490/2575/2492/2493/2455/2544/2486。
- 现状非干净 bug:ENGG4901/4902(Professional Practice A/B)**官方本就是 EquivalenceGroup**(二选一,2u),
  但我库里**又把它们各自当独立 course 重复存了**(equiv + 两个 course 同时存在),计数口径混乱。
- 做法:
  1. 写脚本对**每个工程 plan 页**做"官方学分 vs 我库 `_claimable_codes` 学分"逐规则对账,
     定位每个 34/36 缺的 2u 到底是 (a) equiv 算一份/两份口径、(b) 某课归错规则、(c) 课/units 缺。
  2. 按对账结论分别修:scraper 去重(同课别同时进 equiv 和 course)/ 归位 / 补 units。
  3. 重爬受影响工程程序。
- 验收:逐程序排满,或证明官方该 major 就是 34u(则 require=36 是我 scrape 错,改 require)。

**任务 4:商/理双 major 短 2u**(2564 B.2、2566 B.2.1/B.3.1、2499 B.2)
- 疑似与任务 3 同类(equiv 口径 / attribution)。对账后并入任务 2 或 3 的修法。

### P3 — 数据补全(并入上面重爬)
- 缺课归位:CHEM1100 → 2455 B.1(现错归到规则 I);ENGG4902 → 2569 A.2.1;Civil/Elec/Mechtr/Mech breadth 池补课。
- units 元数据:COMP2200 `units=None`(应 2u),扫一遍 `courses` 表所有 units 为空的课补齐。

---

## 执行顺序与里程碑
1. **P1 任务 2(2460)** — 纯引擎,先做(无网络依赖),回归后并入分支。
2. **P1 任务 1(跨程序)** — 引擎+scraper+补课+重爬;先引擎与 scraper 改好,再小批重爬验证。
3. **P2 任务 3/4** — 写对账脚本批量出诊断,再统一修 scraper + 重爬。
4. **P3** — 随 P2 重爬一并补。
- 每个里程碑跑验证基线;完成率目标逐步逼近 68/68。

## 风险/注意
- 重爬依赖 UQ 外网(可达)+ `program_scraper` / `build_programs` / `build_db` 管线;先在少量程序上验证再批量。
- 后端起服务务必 `nohup ... &` + `disown`(否则后台任务被环境 SIGTERM 回收)。
- 前端正并行迁 HeroUI(另一条线),与本计划互不影响。
