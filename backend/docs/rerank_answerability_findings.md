# Reranker 与 KB 拒答(answerability)调研结论

> 2026-06-13。对应「提高问答准确率」item 2(召回)/ item 3(阈值)。
> 本文只记**调研结论 + 待实现方案**;reranker 与 answerability 门当前**均未实现/未接入**。
> 评测工具:`app/pipelines/{rerank_probe,threshold_scan,kb_eval}.py`,集 `data/eval/kb_refuse.jsonl`。

## 实测数据(本机,kb_chunks 2521 行,bge-m3)

召回 hit@1 / hit@3(英文 golden 45 题):

| 检索 | hit@1 | hit@3 | 中文可用 |
|---|---|---|---|
| bge-m3 基线 | 80% | 98% | ✓ |
| + ms-marco-MiniLM 重排 | 89% | 98% | ✗ 纯英文,坑中文 |
| + bge-reranker-v2-m3 重排 | 84% | 98% | ✓ 多语言 |

拒答可分性(kb_refuse 16 answer / 8 refuse,最优单阈值准确率):bi-encoder 83% | bge-reranker-v2-m3 79%。

## 结论

1. **reranker 治召回,不治拒答。** 虚构实体问题(「申请火星交换生」)与通用「Submit your
   application」页**确实相关**,reranker 给高分(bge-reranker-v2-m3:火星 0.951,高于多个真问题),
   它不核验「这页是否真提到火星交换生」。这是 answerability/faithfulness 问题,不是排序问题。
   （对抗验证补充:把 query 改写成断言式可把探针可分性 79%→88%,但火星仍 0.917 高于 9 个真问题,
   且那已是「用 prompt 做 answerability」,非 rerank;故结论收紧为「没有 rerank 单调变换能把
   半相关虚构实体压到真问题之下」。)

2. **ms-marco-MiniLM 不可用**:纯英文模型把中文真问题打到拒答档(重置密码 ce=-1.9、图书馆 -8.1),
   中文为主的产品会大面积误拒。多语言只能用 bge-reranker-v2-m3 或更轻的 jina-reranker-v2-base-multilingual。

## 推荐

- **reranker:做成可选(env 开关,默认关),16GB 本机不开,不进 student-facing 主链路。**
  理由:`answer_kb` 把 **top-k 整批**喂 qwen,真正决定答案的是 **hit@3=98%(reranker 零提升)**;
  +4pt 全在 hit@1,对多-chunk 生成几乎无增量。为它在 16GB 上常驻 +2.3GB、每查 +0.6~1.2s 不值。
  真要试:先用 `rerank_probe --model jinaai/jina-reranker-v2-base-multilingual`(~1.1GB,CPU 快 ~15×)。

- **拒答(最高价值,red line):做确定性 answerability 门,不是 LLM gate。** 虚构实体本质是
  **该专有名词在 2521-chunk 全语料词频=0** —— 确定性信号,按规则 12 用代码(查词表),不花 LLM 调用 + TTFT。

## 待实现:P0 确定性 answerability 门(本轮未做)

- `pipelines/build_kb_vocab.py`:全语料分词(英文正则 + jieba 中文名词)→ `data/kb/kb_vocab.txt`(词+词频),随入库重建。
- `services/answerability.py`:`answerable(question, chunks) -> (bool, reason)` = 年份越界(2020~2028 外)
  **或** query 主实体在「top-k chunk 文本 ∪ 全语料词集」全部缺席 → 拒。**去掉 sim 豁免**(否则火星 0.951 漏放)。
  词表缺失要抛错,不静默空集(规则 19)。
- 改 `qa._kb_or_none`:检索后过一道 `answerable()`,判否 `return []` + `print` skip 原因;下游 `KB_REFUSE`
  自动接管,answer.py 零改,同步/流式两路复用。
- `pipelines/answerability_eval.py`(复用 kb_refuse):**硬判据 = 8 个虚构全拒、16 个真问题误拒=0**;
  并跑 `answer_eval`/`kb_eval` 确认主链路不被误伤。
- 依赖:jieba(~30MB,纯 CPU)。

## 待实现:P1 reranker 可选骨架 / P2 LLM gate(灰区第二道门)

- P1:`services/reranker.py`(懒加载+单例+失败降级)、`requirements-rerank.txt`(可选依赖)、`kb_search`
  召回 N→可选重排→**min_sim 仍卡 bi-encoder sim**(reranker 分不进拒答判定)。默认关,仅留架构位。
- P2:仅当 P0 评测显示漏网率不可接受(如「太空站实习」这类半相关虚构)才做;代码门控
  `B 放过 且 sim∈[0.62,0.72]` 才调 LLM judge,把调用频率压到最低。

## 残留风险

确定性门治「虚构专有名词」(火星/哈利波特/2099),治不了「实体真存在但语义错配」与「半相关虚构」
(太空站实习);jieba 噪声 + UQ 新项目未收录语料可能误拒真学生 —— **answer 误拒必须靠评测卡到 0**,
不能只看「没崩」。
