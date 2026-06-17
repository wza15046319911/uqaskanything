"""main.py — FastAPI application entry point.

Assembles the QA (/api/ask) and course planner simulator (/api/sim/*) routers.
Frontend and backend are split: the frontend is served by Vite on its own, the
backend only provides a JSON API and no longer serves HTML.

Run:
    uvicorn app.main:app --host 127.0.0.1 --port 8077
    # or  python -m app.main
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
        retrieval.ensure_fts_index(conn)            # build the FTS index once at startup; read path no longer builds it
    uvicorn.run(app, host="127.0.0.1", port=8077)
