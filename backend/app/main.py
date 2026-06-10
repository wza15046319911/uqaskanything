"""main.py — FastAPI 应用入口。

组装问答(/api/ask)与选课模拟器(/api/sim/*)两组路由。前后端分离:
前端由 Vite 独立托管,后端只提供 JSON API,不再托管 HTML。

运行:
    uvicorn app.main:app --host 127.0.0.1 --port 8077
    # 或  python -m app.main
"""
from __future__ import annotations

import psycopg
from fastapi import FastAPI

from app.core.config import DSN
from app.api import ask, sim
from app.services import retrieval

app = FastAPI(title="UQ Course QA")
app.include_router(ask.router)
app.include_router(sim.router)


if __name__ == "__main__":
    import uvicorn
    with psycopg.connect(DSN) as conn:
        retrieval.ensure_fts_index(conn)            # 启动时建一次 FTS 索引,读路径不再建
    uvicorn.run(app, host="127.0.0.1", port=8077)
