"""问答 API（POST /api/ask）。

自然语言问题 -> qa.run 检索 + grounded 生成 -> JSON。
"""
from __future__ import annotations

import psycopg
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core.config import DSN
from app.services import qa

router = APIRouter()


class Ask(BaseModel):
    question: str
    generate: bool = True


@router.post("/api/ask")
def ask(body: Ask):
    q = body.question.strip()
    if not q:
        return JSONResponse({"error": "问题不能为空"}, status_code=400)
    try:
        with psycopg.connect(DSN) as conn:
            res = qa.run(conn, q, generate=body.generate)
        res.pop("plan", None)                      # plan 含内部细节,前端用不到
        return res
    except Exception as e:                          # 不吞错:错误信息返回给前端展示
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
