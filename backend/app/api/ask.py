"""问答 API（POST /api/ask）。

自然语言问题 -> qa.run 检索 + grounded 生成 -> JSON。
"""
from __future__ import annotations

import json

import psycopg
from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
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


@router.post("/api/ask/stream")
def ask_stream(body: Ask):
    """SSE 流式问答:先发 meta(结构化课程),再逐 token 流式答案,最后 done(护栏后全文)。
    每行 `data: {"type": meta|token|done|error, "data": ...}\\n\\n`。"""
    q = body.question.strip()
    if not q:
        return JSONResponse({"error": "问题不能为空"}, status_code=400)

    def event_stream():
        try:
            with psycopg.connect(DSN) as conn:
                for event, data in qa.run_stream(conn, q):
                    yield f"data: {json.dumps({'type': event, 'data': data}, ensure_ascii=False)}\n\n"
        except Exception as e:                      # 不吞错:把错误作为一个 SSE 事件发给前端
            payload = {"type": "error", "data": f"{type(e).__name__}: {e}"}
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
