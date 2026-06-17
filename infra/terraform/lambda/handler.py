"""Signing proxy: browser REST -> SigV4 invoke_agent_runtime -> AgentCore Runtime.

The browser cannot call AgentCore directly (SigV4 + AWS creds). CloudFront routes
/api/* here; this Lambda signs the call with its IAM role and returns the result.

Handles two paths so the existing frontend needs no change:
  - /api/ask          -> pass the agent JSON through verbatim
  - /api/ask/stream   -> wrap the single result into meta/token/done SSE events
                         (no real streaming: the answer arrives at once)

CloudFront injects x-origin-secret on every origin request; requests without the matching
secret (e.g. direct Function URL hits) are rejected with 403 before any agent call.

env: AGENT_RUNTIME_ARN, BEDROCK_REGION, ORIGIN_SECRET
"""
import base64
import hmac
import json
import os

import boto3
from botocore.config import Config

ARN = os.environ["AGENT_RUNTIME_ARN"]
REGION = os.environ.get("BEDROCK_REGION", "us-east-1")
ORIGIN_SECRET = os.environ.get("ORIGIN_SECRET", "")
_client = None


def _agent():
    global _client
    if _client is None:
        _client = boto3.client(
            "bedrock-agentcore", region_name=REGION,
            config=Config(read_timeout=120, connect_timeout=10, retries={"max_attempts": 1}),
        )
    return _client


def _invoke(question, request_id):
    sid = (request_id + "0" * 48)[:48]  # AgentCore requires runtimeSessionId >= 33 chars
    r = _agent().invoke_agent_runtime(
        agentRuntimeArn=ARN,
        runtimeSessionId=sid,
        payload=json.dumps({"question": question}).encode("utf-8"),
        contentType="application/json",
        accept="application/json",
    )
    body = r["response"].read()
    return body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body


def _json(code, obj):
    return {"statusCode": code, "headers": {"content-type": "application/json"},
            "body": json.dumps(obj, ensure_ascii=False)}


def _sse(result):
    res = json.loads(result)
    meta = {k: res.get(k) for k in ("mode", "meta", "courses", "program_facts", "chunks", "course")}
    answer = res.get("answer") or res.get("error") or ""
    events = (
        f"data: {json.dumps({'type': 'meta', 'data': meta}, ensure_ascii=False)}\n\n"
        f"data: {json.dumps({'type': 'token', 'data': answer}, ensure_ascii=False)}\n\n"
        f"data: {json.dumps({'type': 'done', 'data': answer}, ensure_ascii=False)}\n\n"
    )
    return {"statusCode": 200, "headers": {"content-type": "text/event-stream"}, "body": events}


def lambda_handler(event, context):
    headers = event.get("headers") or {}
    provided = headers.get("x-origin-secret", "")
    if not ORIGIN_SECRET or not hmac.compare_digest(provided, ORIGIN_SECRET):
        return _json(403, {"error": "forbidden"})
    path = event.get("rawPath") or event.get("requestContext", {}).get("http", {}).get("path") or ""
    raw = event.get("body") or ""
    if event.get("isBase64Encoded") and raw:
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        body = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return _json(400, {"error": "invalid JSON body"})
    question = (body.get("question") or "").strip()
    if not question:
        return _json(400, {"error": "question must not be empty"})
    try:
        result = _invoke(question, context.aws_request_id)
    except Exception as e:
        return _json(500, {"error": f"{type(e).__name__}: {e}"})
    if path.endswith("/stream"):
        return _sse(result)
    return {"statusCode": 200, "headers": {"content-type": "application/json"}, "body": result}
