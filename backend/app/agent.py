"""agent.py — AgentCore Runtime entrypoint (QA only, no simulator).

Wraps the QA logic qa.run as the AgentCore Runtime entrypoint: BedrockAgentCoreApp
serves 0.0.0.0:8080 with /invocations + /ping; this module only forwards, does not go
through FastAPI, and does not include sim (the simulator is out of scope here). First
version is non-streaming; streaming is a follow-up (add a qa.run_stream yield entrypoint
reusing the {"type":event,"data":data} shape).

Usage:
    python -m app.agent          # start locally / in container, listens on 8080
    # call: POST /invocations  body={"question": "..."}

env (reused from the qa path, injected into the container as env vars):
    DATABASE_URL                 RDS connection string (read by config.py)
    LLM_BACKEND=bedrock          use Bedrock gpt-oss
    BEDROCK_MODEL / BEDROCK_REGION
    EMBED_BASE / EMBED_MODEL / EMBED_API_KEY   DeepInfra bge-m3
"""
from __future__ import annotations

import psycopg
from bedrock_agentcore import BedrockAgentCoreApp

from app.core.config import DSN
from app.services import qa

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload: dict) -> dict:
    """AgentCore entrypoint: take question -> qa.run -> return the structured result (drop internal plan).
    Do not swallow errors: like ask.py, return {"error": "<type>: <msg>"} to the caller."""
    question = (payload.get("question") or payload.get("prompt") or "").strip()
    if not question:
        return {"error": "question must not be empty"}
    try:
        with psycopg.connect(DSN) as conn:
            res = qa.run(conn, question)
        res.pop("plan", None)
        return res
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


if __name__ == "__main__":
    app.run()
