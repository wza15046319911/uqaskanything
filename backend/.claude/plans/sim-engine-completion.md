# 选课模拟器引擎 — 收尾计划(冲刺"完美无瑕")

## 目标
68 个 EAIT 程序在模拟器里都能被真实学生排满到 `formula_satisfied=True`;凡排不满的,
必须能明确解释成"真实学制约束"而非引擎/数据缺陷。

## 当前状态(2026-06,分支 `fix/sim-rule-engine`)—— 目标达成
- 已修 **11 个引擎 bug + 1 次架构重构**(详见 memory `sim-rule-engine.md`)。本轮 4 项:
  - **P1-2 认领层 plan 感知 required**(`_effective_required`):救活 2460 数学单(commit a0e34ba)。
  - **P1-1 跨程序 Program 引用展开**(`_expand_program_refs`):救活工程类 + 5257/2560(commit 2594fa3)。
  - **无上限纯 wildcard 按开放规则**:救活 2557 A.6 自由选修(commit fcf1483)。
  - **统一全局归属重构**(`_assign`,commit b86a018):把"顶层认领 + plan 内分摊"两层合并成
    一次叶子级全局增广匹配(equiv 折叠 + 降序装箱),修掉 plan 内重复计数 + 子规则 cap 浪费,
    工程 discipline major A.x.1 从 34/36 真实修到 36。
- 广测 68 个 EAIT 程序:**0 崩溃 / 0 静默丢课 / pytest 42 过**。
- **学科感知完成器:67/68 自动排满**;唯一剩 2557 = A.6"任选 2u 自由选修",引擎正确计入
  (手动补任意 2u 即 `formula_satisfied=True`),启发式猜不出"随便哪门"但真实学生一点即可。
- **结论:全 68 个 EAIT 程序引擎层面均可排满到学位要求满足**(原 42→49→63→**67/68 自动、
  实质 68/68 引擎正确**)。目标"完美无瑕"达成。

## 验证基线(每步必做)
- `cd backend && PYTHONPATH=. python3 -m pytest -q`(须保持 42 过)。
- `python3 /tmp/uqsim/batch.py`(完成率;每步不得回退)。
- 抽改动相关程序,对官方 `programs-courses.uq.edu.au`(数据内联在 `window.AppData`)逐规则核对。
- 工具:`/tmp/uqsim/{sim.py 调 API, batch.py 完成率, maxc.py 激进全选, official.py 官方逐规则/plan 池}`。

---

## P1 已完成(本轮)

- **P1-2 / 2460**:`_effective_required` —— 认领层(`_claim_slack`/`_claims`)原用 `_required`
  只读原始 `units_min`,对 from-plans 选修(自身 None、需求来自所选 major)当成 required=0,
  被同码低 min 规则抢光。抽出 plan 感知的 required 给认领层定优先级。✅ commit a0e34ba。
- **P1-1 / 跨程序**:`_expand_program_refs` —— init 时把「Program 型引用」(空 rules,指向整
  program)就地解析成被引 program 的课程池;嵌套在 plan 内的引用剔除该 plan 已列课防
  `_plan_units_done` 重复计数。被引 program(2455/2559/5257/2350/2320)及其课**都在库,不用重爬**。
  ✅ commit 2594fa3。救活工程类一大批 + 5257/2560。
- **2557 A.6 wildcard**:`_open_rule` 放开「无上限纯 wildcard」自由选修。✅ commit fcf1483。

完成率 42→49→**63/68**;pytest 42 过,0 崩溃 0 静默丢课。

> 注:原计划把"5257 的 12 门课不在库""ENGG4901/4902 equiv 重复存"列为数据缺口 —— 经本轮核查
> **均不成立**(课都在库;34/36 不是 equiv 口径问题)。下方根因已更新。

---

## 统一全局归属重构(已落地,commit b86a018)

把"顶层 `_claims` 认领 + plan 内 `_plan_units_done` 各自封顶求和"两层,合并成一次跨全部
**叶子规则**(顶层非父规则 + 已选 plan 内部子规则)的全局增广匹配 `_assign`:
- 每门已选课唯一归属到一个叶子 bin(`bin_id=id(规则)`),各 bin 不超自身 `units_max`,
  按顶层规则紧度让尽量多顶层规则达 `_effective_required`;计数层 `_rule_units_done`/
  `_plan_units_done`/`_item_done_units` 改按 `self._bin_of` 判归属。
- 修掉两层割裂的两个老问题:plan 内同一课被多条子规则**重复计数**;顶层认领看不到子规则
  cap、课堆进有上限子规则被截而其它空着、major **计不满**。
- 两个关键细节:bin 收集时 **equivalence 组折叠成一个代表**(口径同 `_item_done_units`,
  否则 CIVL4516/4518 当两门 → 虚高 2u 提前停);候选课**按 units 降序填**(大学分先填,
  避免 4u 研究课被 2u 课挤出 max4 的 bin → 装箱次优)。
- 效果:工程 major A.x.1 从 34/36 修到真实 36。

### 试过但已还原的两个方向(留痕,勿重蹈)
- **只改 `_plan_units_done` 做 plan 内 greedy**:不动认领层 → A.3.1 32→30 反而更差(认领层给
  A.3.1 的课本就不含能填 C/D 的),证明必须动认领层本身。已还原。
- **父规则需求向下传播 `_leaf_demands`**(让 A.3.4 这类"父 min16 > 子 min8"的填满):
  给每个 OR 分支都发满需求 → OR 兄弟分支(A.3.2/3/4 只需其一)疯狂抢课,A.3.1 退回 32、
  2460 也被带崩(63→60)。OR 竞争下的需求分配不可控,已还原。父规则填不满的情形改由
  **选课层**解决(真实学生择一 OR 分支并补够),而非认领层硬塞。

---

## 剩余 1 个(2557)+ 可选数据补全

- **2557 A.6**:纯 wildcard"任选 2u 自由选修"。引擎正确(补任意 2u → done),仅自动启发式
  猜不出选哪门。如要让 harness/UI 也能一键完成,给开放 wildcard 规则做"自动补一门有效课"
  的 UX,而非改引擎。
- **数据补全(P3,非引擎必需)**:`COMP2200` 等少量课不在 `courses` 表(引擎已用规则 item 自带
  units 兜底计数,不挡完成);为数据质量(标题/开课/先修)可补爬。扫 `courses` units 为空的课补齐。

## 验证基线(每步必做)
- `cd backend && PYTHONPATH=. python3 -m pytest -q`(保持 42 过)。
- 学科感知完成器(择一学科 + 配套 minor/数学 major 选 plan,填该线可认领课)应保持 67/68。
- 后端起服务务必 `nohup ... &` + `disown`(否则后台任务被环境 SIGTERM 回收);改引擎后
  `pkill -f "uvicorn ... 8077"` 重启(无 --reload)。
- 工具:`/tmp/uqsim/{sim.py, batch.py, maxc.py, official.py}`(注意 batch/maxc 的自动选课器会
  选所有学科 plan,**低估**引擎,不代表真实学生择一)。

## 风险/注意
- `_assign` 是全引擎最核心、最易回归处(所有含 plan 的 program 都过它);任何改动须跑 pytest 42 +
  学科感知完成器全量。
- 前端正并行迁 HeroUI(另一条线),与本计划互不影响。
