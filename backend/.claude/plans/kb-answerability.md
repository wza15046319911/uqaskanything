# KB 拒答(answerability)+ reranker — 实现计划

> 调研已完成、实现暂缓(用户决定)。调研数据/结论见 `docs/rerank_answerability_findings.md`。
> 本文是**落地路线**:成功判据 + 逐文件步骤 + 验证基线。分支 `fix/sim-rule-engine`。

## 目标

KB 问答守住 student-facing 红线 3「refuse over wrong」:**虚构实体问题(「申请火星交换生」
「哈利波特学院课程」)必须拒答,而不是拿通用官方页编一套**;同时**绝不误拒真学生的真问题**。
成功 = `answerability_eval` 上 8 个虚构全拒、16 个真问题误拒 = 0,且 `answer_eval`/`kb_eval` 不回退。

## 当前状态(2026-06-13)

- **拒答门槛已数据调优**:`retrieval.kb_search` min_sim 0.55→0.62(commit ef6d301,`threshold_scan` 扫出)。
- **reranker 调研定论**:治召回不治拒答(bge-reranker-v2-m3 重排后火星仍 0.951 高于真问题)。
  推荐**可选默认关**,本机 16GB 不开。详见 findings 文档。
- **不可约缺口仍在**:虚构实体 sim 高于真问题,纯阈值/纯 reranker 都分不开。本计划的 P0 是对症解。
- 现成评测:`data/eval/kb_refuse.jsonl`(16 answer / 8 refuse)、`answers.jsonl`、`routing.jsonl`;
  工具 `pipelines/{rerank_probe,threshold_scan,kb_eval,answer_eval,route_eval}.py`。

## 验证基线(每步必做)

- `cd backend && python -m pytest -q`(当前 49 过,不得回退)。
- `python -m app.pipelines.answer_eval`(12 题,真问题带来源、不误拒)。
- `python -m app.pipelines.kb_eval --chunks data/kb/chunks_all.jsonl`(hit@1 80% / hit@3 98% 不回退)。
- 需 Postgres:5433 + Ollama bge-m3。改 KB 路由后重点盯**误拒**(answer 题被错拒是红线)。

---

## P0 — 确定性 answerability 门(核心,先做)

**判据**:虚构专有名词在 2521-chunk 全语料**词频=0** → 这是确定性信号,按规则 12 用代码查词表,
不花 LLM 调用 + TTFT。**去掉 sim 豁免**(否则火星 0.951 被高 sim 漏放)。

1. `pipelines/build_kb_vocab.py`(新):全量 `kb_chunks.text` 分词(英文正则 + jieba 中文名词)→
   `data/kb/kb_vocab.txt`(词 + 词频)。随入库重建(并入 kb_build 后或单独跑)。依赖 **jieba(~30MB,纯 CPU)**。
2. `services/answerability.py`(新):`answerable(question, chunks) -> (bool, reason)`。
   判否(拒答)= **年份越界**(出现 2020~2028 外的学年年份)**或** query 主实体(专有名词/关键名词)
   在「top-k chunk 文本 ∪ 全语料词集」**全部缺席**。词表文件缺失要**抛错**,不静默空集(规则 19)。
3. 改 `services/qa.py::_kb_or_none`:`kb_search` 后过一道 `answerable()`,判否 `return []` +
   `print` skip 原因(不静默)。下游 `answer.answer_kb`/`run_stream` 的 `KB_REFUSE` 自动接管 ——
   **answer.py 零改,同步/流式两路复用**。
4. `pipelines/answerability_eval.py`(新):复用 `kb_refuse.jsonl`。
   **硬判据:8 个虚构尽量全拒(火星/太空站/哈利波特/滑雪/2099/…)、16 个真问题误拒 = 0。**
   误拒=0 是红线;虚构漏网的(如「太空站实习」)记下来,作为是否上 P2 的依据。

## P1 — reranker 可选骨架(默认关,仅留架构位)

仅在 P0 完成且评测通过后做;**默认不开,不进 student-facing 主链路**。

1. `services/reranker.py`(新):懒加载 + 进程内单例 + 失败降级(导入失败/OOM 时退回 bi-encoder)。
2. `requirements-rerank.txt` 已存(可选依赖)。base 安装不得碰 torch。
3. 改 `retrieval.kb_search`:env 开关(如 `KB_RERANK=1`)→ bge-m3 取 top-N(N≈20)→ 重排 → top-k;
   **min_sim 仍卡 bi-encoder sim,reranker 分绝不进拒答判定**(拒答归 P0)。
4. 选型:接前先用 `rerank_probe --model jinaai/jina-reranker-v2-base-multilingual`(~1.1GB,CPU 快 ~15×)
   在 kb_refuse 上跑一轮,大概率内存/延迟减半、hit@1 相近。
5. `pytest` 验证:不设 `KB_RERANK` 时 import 不碰 torch、行为与现状完全一致。

## P2 — LLM gate 灰区第二道门(条件触发,暂不做)

仅当 P0 评测显示漏网率不可接受(尤其「实体真存在但语义错配」「半相关虚构」如太空站实习)才做。
触发用代码门控死:**`P0 放过 且 bi-encoder sim ∈ [0.62, 0.72]`** 才调 `kb_answerable()`(qwen 判
「这段官方资料是否真回答了这个具体问题/实体」),把 LLM 调用频率压到最低。挂 `_kb_or_none`(两路复用)。
judge 异常时倾向**放行**(有下游官方链接兜底,避免 Ollama 偶发超时批量误拒真学生)。

---

## 调研留痕(勿重蹈)

- **ms-marco-MiniLM 不可用**:纯英文,把中文真问题打到拒答档(重置密码 ce=-1.9、图书馆 -8.1),
  中文产品会大面积误拒。多语言才行。
- **reranker 不治拒答**:它衡量 query–passage 相关性,「申请火星交换生」与通用申请页确实相关,
  reranker 给高分;不核验实体存在。别指望 reranker 解决拒答。
- **断言式 query 改写**把探针可分性 79%→88%,但火星仍 0.917 高于 9 个真问题,且那已是「用 prompt 做
  answerability」——不是 rerank 的功劳,生产别为它接 reranker。
- **naive token-overlap 会坑中文**:中文真问题易被误压;实体缺席判定要用分词后的「专有名词缺席」,
  不是朴素子串包含。

## 风险/注意

- **误拒真学生是头号风险**:jieba 分词噪声 + UQ 新项目语料未收录,可能把真问题判成虚构。
  `answerability_eval` 的「16 真问题误拒=0」是硬门,过不了不 ship。
- P0 改的是 KB 兜底层;课程主链路(filter/semantic/hybrid/program)不受影响,但仍要跑 `answer_eval`
  确认 KB 正常答题没被误伤。
- 词表随语料更新会变(新增 FAQ → 新词);`build_kb_vocab` 要并进 KB 重建流程,别让词表过期把新页问题误拒。
