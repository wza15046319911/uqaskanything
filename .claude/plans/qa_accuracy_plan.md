# 问答准确率提升计划

> 目标:系统性提升 UQ 课程问答的准确率。
> 依据:基于当前系统实测观察到的失败点(`query.py`:本地 qwen2.5-coder LLM-to-SQL + bge-m3 向量 + Postgres),不是泛泛 RAG 建议。

## 0. 当前已知失败点(实测)

- **路由**:「计算机相关」→ 被误判成 `title LIKE '%计算机%'`(中文匹配英文课名 → 0 命中);「CS」→ 主题词被直接丢弃
- **语义噪声**:「计算机相关、无 hurdle」把 Computational Physics 排在核心 COMP 课之前
- **无阈值**:固定返回 top-8,尾部弱匹配(sim≈0.28)也返回

根因:本地 7B 不会推理路由,全靠 few-shot 补;向量检索缺重排和关键词信号。

---

## 1. 查询规划层(LLM-to-SQL)— 最大瓶颈

- **换更强模型做这一步**(DeepSeek API / 更大本地模型)→ 路由自动泛化,不用逐个补例子。**单点 ROI 最高**。
- **结构化输出**:用 JSON schema 约束(不只 `format=json`),强制 mode/where/semantic_query 合法。
- **计划校验(确定性兜底)**:LLM 出计划后用代码检查 —— 如「问题含主题词但 semantic_query 为空」则重试/补救。
- **拆分**:路由判断 与 写 SQL 拆成两个更简单的调用。

## 2. 向量检索质量

- **加 reranker**(bge-reranker 等)对 top-k 重排 → 精度提升最明显、成本低。**优先做**。
- **关键词 + 向量混合**:名字里就叫 "Machine Learning" 的课应靠 Postgres 全文检索排前,与向量分数做 RRF 融合 → 修「核心课排不上去」。
- **相似度阈值**:加相对/绝对门槛,砍掉弱匹配尾部。
- **优化 `search_blob`**:embedding 上限由它决定;可加权 title、补 coordinating_unit(学院)。

## 3. 答案生成层(目前缺失)

- 现在只返回课程列表,不算真「问答」。把检索结果 + 问题喂 LLM **生成有依据的 NL 回答 + 课程码引用**,约束只用检索结果防幻觉。
- 解锁「比较 X 和 Y」「推荐学习路径」这类非纯检索问题。

## 4. 评测闭环(地基)

- 现在是手动抽查。建一组 **gold 测试集**(问题 → 期望结果),量化 precision/recall@k。
- **没有度量,换模型/改 prompt/加 reranker 哪个有用都说不清。**

## 5. 数据质量与覆盖(上游天花板)

- 只抓了 S1 / St Lucia / In Person → 问 S2、其他校区直接空。扩覆盖 = 扩可答范围。
- 接入 program 维度(已抓)→ 能答「这门课是哪些专业必修」。
- 派生字段、search_blob 质量 = 检索上限。

## 6. 结构化字段扩展

- filter 现在只能用枚举列。加可过滤字段(学院/school、课程码提取的 level 数字、prerequisite)→ 更多问题走精确路径,不必赌语义。

---

## 7. 实施 Roadmap(按 ROI)

- [x] **阶段一**:评测集 `eval.py`(19 gold + 6 回归断言;路由/filter/语义recall/program 全 100%)
- [x] **阶段二**:关键词+向量 RRF 混合检索 `retrieval.py`(FTS GIN 索引)+ 相似度阈值 0.45(reranker 可后续加)
- [~] **阶段三**:planner 结构化输出 + 确定性计划校验 + 枚举守卫已做;DeepSeek 后端写好可插拔(默认本地 qwen,配 `DEEPSEEK_API_KEY` 即用)
- [x] **阶段四**:答案生成层 `answer.py`(grounded + 课码引用 + 越界护栏 guard_citations)
- [~] **阶段五**:program 维度已接入问答(qa.py program 模式);数据覆盖(S2/其他校区)待扩

### 已落地架构(对应上面)

`qa.py` 编排:`planner.plan`(NL→计划,4 模式 + 确定性兜底)→ `retrieval`(filter/semantic/hybrid,RRF 融合)/ `program_lookup`(课↔专业)→ `answer.answer`(grounded + 护栏)。评测 `eval.py` 锁住路由/检索/program + 6 条对抗回归断言。
