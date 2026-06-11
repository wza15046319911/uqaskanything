# 选课模拟器引擎 — 收尾计划(冲刺"完美无瑕")

## 目标
68 个 EAIT 程序在模拟器里都能被真实学生排满到 `formula_satisfied=True`;凡排不满的,
必须能明确解释成"真实学制约束"而非引擎/数据缺陷。

## 当前状态(2026-06,分支 `fix/sim-rule-engine`)
- 已修 **10 个引擎 bug**(详见 memory `sim-rule-engine.md`):
  前 7 个(嵌套上卷、OR-done branchable、共享课增广匹配、畸形 rule_logic、负 semIndex、
  augment 非均匀学分腾挪、from-plans 错继承 plan min);本轮再修 3 个:
  - **P1-2 认领层 plan 感知 required**(`_effective_required`):救活 2460 数学单(commit a0e34ba)。
  - **P1-1 跨程序 Program 引用展开**(`_expand_program_refs`):救活工程类 + 5257/2560
    跨程序双学位(commit 2594fa3)。
  - **无上限纯 wildcard 按开放规则**:救活 2557 A.6 自由选修(commit fcf1483)。
- 广测 68 个 EAIT 程序:**0 崩溃 / 0 静默丢课 / pytest 42 过**;**63/68 可排满**(原 42→49→63)。
- 剩余 **5 个**排不满。根因已精确定位(见下):**不是数据,是引擎两层归属的局限**。

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

## 剩余 5 程序 · 根因(已精确定位:引擎,非数据)

### 核心限制:两层归属无法对 plan 内部子规则的 cap 做全局最优

`_claims` 在**顶层规则 ref 粒度**把共享课认领给某顶层规则(如工程 major `A.3.1`),
但 `_plan_units_done` 再把这些课摊到该 plan 的**内部子规则**(核心 all + 研究 max4 +
进阶 max4 + program 电选无上限)时,认领层选"给 A.3.1 哪些课"时**看不到**这些子规则 cap,
给的课分布不好 → 课堆进有上限的子规则被截掉、其它子规则空着 → major 计不满(34/36)。
另外 `_plan_units_done` 现实现按子规则**各自封顶求和**,同一门课若被多条子规则列出会**重复计数**。

- **工程双学位 A.x.1=34/36**(2544/2492/2493 等):上述限制。一个真实学生**选对课**(把电选填进
  无上限子规则而非堆进 max4 的)能排满 36;问题是引擎/harness 的自动归属次优,**不是不可完成**。
- **2566 商/理双**(B.2.1/B.3.1 短 2u):同类。
- **2557**:仅 A.6=0/2,**纯 wildcard 自由选修,引擎已正确**(手动补任意 2u 即 done);
  harness 无法自动挑自由选修课,是 harness 局限不是引擎缺陷。

### 真正修复 = 统一全局归属(比 P1 大,需单独立项)

把"顶层认领 + plan 内分摊"两层合并成**一次跨全部叶子规则(含 plan 子规则展开)的全局增广匹配**,
每门课唯一归属、各叶子规则不超自身 cap、最大化已满足规则数。
- 本轮试过的**局部修**(只把 `_plan_units_done` 改成 plan 内 greedy 最优分配):pytest 过、batch 仍
  63/68 **不回退也不前进**(A.3.1 32→30,更不重复计数但 greedy 次优),且不修好任何 program →
  已还原(遵守"最小改动/不过度工程")。证明**必须动认领层本身**,局部修无效。
- 风险:认领层是全引擎最核心、最易回归处(所有含 plan 的 program 都过它);需大量回归。

---

## 执行顺序与里程碑(更新)
1. ✅ P1-2(2460)、P1-1(跨程序)、2557 wildcard —— 已并入分支。
2. **统一全局归属重构** —— 唯一通往 68/68 的路;先在 2492/2544/2566 上验证设计,再全量回归。
   是独立的大改,建议确认后单独立项。
3. 验证基线每步必跑;harness 需补"自由选修自动补课"能力才能把 2557 也算进自动完成。

## 风险/注意
- 重爬依赖 UQ 外网(可达)+ `program_scraper` / `build_programs` / `build_db` 管线;先在少量程序上验证再批量。
- 后端起服务务必 `nohup ... &` + `disown`(否则后台任务被环境 SIGTERM 回收)。
- 前端正并行迁 HeroUI(另一条线),与本计划互不影响。
