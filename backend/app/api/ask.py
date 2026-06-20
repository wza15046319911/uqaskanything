"""QA API (POST /api/ask).

Natural language question -> qa.run retrieval + grounded generation -> JSON.
"""
from __future__ import annotations

import json

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from app.core.config import DSN
from app.core import ratelimit
from app.services import qa

router = APIRouter()


class Ask(BaseModel):
    question: str
    generate: bool = True


@router.post("/api/ask")
def ask(body: Ask, request: Request):
    blocked = ratelimit.check(request)
    if blocked is not None:
        return blocked
    q = body.question.strip()
    if not q:
        return JSONResponse({"error": "问题不能为空"}, status_code=400)
    try:
        with psycopg.connect(DSN) as conn:
            res = qa.run(conn, q, generate=body.generate)
        res.pop("plan", None)                      # plan holds internal details the frontend does not need
        return res
    except Exception as e:                          # do not swallow errors: return the message for the frontend to show
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)


@router.post("/api/ask/stream")
def ask_stream(body: Ask, request: Request):
    """SSE streaming QA: first send meta (structured courses), then stream the answer token by token, finally done (full text after guards).
    Each line is `data: {"type": meta|token|done|error, "data": ...}\\n\\n`."""
    blocked = ratelimit.check(request)
    if blocked is not None:
        return blocked
    q = body.question.strip()
    if not q:
        return JSONResponse({"error": "问题不能为空"}, status_code=400)

    def event_stream():
        try:
            with psycopg.connect(DSN) as conn:
                for event, data in qa.run_stream(conn, q):
                    yield f"data: {json.dumps({'type': event, 'data': data}, ensure_ascii=False)}\n\n"
        except Exception as e:                      # do not swallow errors: send the error to the frontend as one SSE event
            payload = {"type": "error", "data": f"{type(e).__name__}: {e}"}
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
