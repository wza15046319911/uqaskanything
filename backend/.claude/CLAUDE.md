# UQ Course RAG — Backend

FastAPI 后端:课程问答(RAG)+ 选课模拟器 API。前端在 `../frontend`(Vite+React)。

## 结构
- `app/main.py` — FastAPI 入口,只组装路由 + 启动
- `app/api/` — 路由:`ask.py`(问答)、`sim.py`(模拟器)
- `app/services/` — 业务逻辑:qa / retrieval / planner / simulator / scheduler / sim_advise / llm / answer / program_lookup / query
- `app/scrapers/` — UQ 课程/培养方案爬虫(CLI)
- `app/pipelines/` — 入库 / embedding(CLI)
- `app/core/config.py` — DSN、数据目录、S2 开课码(唯一配置源)
- `data/` — 课程/培养方案 JSONL 等
- `tests/` — pytest
- `docs/` — 抓取/构建进度记录

## 运行(均从 backend/ 目录)
- API:`uvicorn app.main:app --port 8077`
- 测试:`pytest`
- 数据管线:`python -m app.pipelines.build_db`、`python -m app.scrapers.scraper` 等

## 栈
Postgres + pgvector(:5433);Ollama(bge-m3 向量 + qwen2.5-coder 生成),
设了 `DEEPSEEK_API_KEY` 则改走 DeepSeek。配置见 `.env`(参考 `.env.example`)。
