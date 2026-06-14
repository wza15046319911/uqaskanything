# RAG 评测(eval/)

对 `/api/ask` 这条 RAG 链路跑 LLM-as-judge 指标(量化幻觉与检索质量),与
`backend/app/pipelines/` 里那套**确定性**评测互补:那套断言来源/课码集合/拒答(红线
1/2/3),这套量化 faithfulness / relevancy / context precision。

两套 judge 框架,共用同一份样本(`generate.py` 产的 `data/generated.jsonl`)对照跑:
- **RAGAS**(`ragas_*.py`):judge=DeepSeek,embedding=本地 bge-m3。
- **DeepEval**(`deepeval_*.py`):judge=DeepSeek(deepeval 4.x 内置 `DeepSeekModel`),
  纯 LLM 判分。换实现交叉验证,抓单一框架的系统性偏差。

> 两者依赖冲突(deepeval 要 `openai>=2`、ragas 的 langchain 要 `openai<2`),**各用独立
> venv**:`eval/.venv` 跑 ragas,`eval/.venv-deepeval` 跑 deepeval。

- judge LLM：DeepSeek（OpenAI 兼容端点）
- embedding：本地 Ollama `bge-m3`（与生产检索同模型）
- 取数：通过 HTTP 调后端 `/api/ask`，不 import backend，依赖与后端运行时解耦

## 安装

独立虚拟环境,别装进后端环境;两套各一个 venv,共用一份 `.env`:

```bash
cp eval/.env.example eval/.env   # 填 DEEPSEEK_API_KEY(两套共用)

# RAGAS(langchain/datasets 较重)
python3 -m venv eval/.venv
eval/.venv/bin/pip install -r eval/requirements.txt

# DeepEval(openai>=2,与上面冲突,必须独立 venv)
python3 -m venv eval/.venv-deepeval
eval/.venv-deepeval/bin/pip install -r eval/requirements-deepeval.txt
```

## 跑评测

前置:后端在 `BACKEND_URL`(默认 `127.0.0.1:8077`)上跑、Postgres+KB 已灌库、本地
ollama 已起 `bge-m3`。`generate.py` 产的样本两套共用,只需产一次。

```bash
eval/.venv/bin/python eval/generate.py            # 调后端逐题取「答案+检索上下文」-> data/generated.jsonl

eval/.venv/bin/python eval/ragas_eval.py          # RAGAS  -> reports/ragas_report.json
eval/.venv-deepeval/bin/python eval/deepeval_eval.py  # DeepEval -> reports/deepeval_report.json
```

## 文件

- `questions.jsonl`(`data/`):种子题,只放走 LLM 的 mode(kb/course_detail/semantic/
  hybrid)。可加 `"reference": "..."` 标准答案,会自动多跑 recall/precision 类指标。
- `generate.py`:HTTP 取数,按 mode 抽 contexts,落 `data/generated.jsonl`(两套共用)。
- `ragas_config.py` / `ragas_eval.py`:RAGAS 的 judge+embedding 装配、跑指标。
- `deepeval_config.py` / `deepeval_eval.py`:DeepEval 的 DeepSeek judge 装配、跑指标。

DeepEval 指标:`Faithfulness` / `AnswerRelevancy` / `ContextualRelevancy` 默认跑;样本带
`reference` 时再加 `ContextualPrecision` / `ContextualRecall`。

## 注意

- `program` / `empty` 是确定性答案、无检索上下文,两套都不适用——会被剔除并在输出里计数。
- judge 是 LLM,分数有小幅波动;看趋势/抓回归,不是逐位回归。
- 两个 `requirements*.txt` 的版本钉死值未在本机联网验证;装不上时分别以 RAGAS 0.2.x /
  DeepEval 4.0.x 文档为准微调。
