"""llm.py — pluggable LLM backend (shared by planner routing + answer generation).

Backend choice:
  - LLM_BACKEND=bedrock      -> Amazon Bedrock (gpt-oss, InvokeModel OpenAI-compatible)
  - DEEPSEEK_API_KEY is set  -> use DeepSeek (planner + answer, all the way)
  - not set                  -> local Ollama qwen
  - LLM_ENABLED=false        -> force local Ollama (even with a key, for a temporary fallback)
An explicit LLM_BACKEND (bedrock/deepseek/ollama) wins; if empty, fall back to the key detection above.

.env: loads the .env in the same directory automatically on import (has a minimal
parser built in, no python-dotenv needed); real environment variables win and are
not overwritten by .env. Just put the key in .env, do not commit it to git.

env:
  LLM_BACKEND       explicit backend: bedrock/deepseek/ollama; empty = auto by key (original behavior)
  BEDROCK_MODEL     Bedrock model id, default openai.gpt-oss-120b-1:0 (set -20b to save cost)
  BEDROCK_REGION    Bedrock region; empty falls back to AWS_REGION / default credential chain
  DEEPSEEK_API_KEY  DeepSeek key (decides whether to use DeepSeek)
  DEEPSEEK_BASE     default https://api.deepseek.com
  DEEPSEEK_MODEL    default deepseek-chat
  OLLAMA_URL        default http://localhost:11434
  LLM_MODEL         local model, default qwen2.5-coder:7b
  LLM_ENABLED       default true; set 'false' to force local Ollama (do not use DeepSeek even with a key)
"""
from __future__ import annotations
import os
import re
import json
import pathlib
from collections.abc import Iterator

import requests


def _load_dotenv(name: str = ".env") -> None:
    """Minimal .env loader: KEY=VALUE per line; # comments; real env vars win (setdefault, no overwrite)."""
    p = pathlib.Path(__file__).resolve().parents[2] / name
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):           # tolerate the `export KEY=VALUE` form
            line = line[len("export "):]
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("LLM_MODEL", "qwen2.5-coder:7b")
DEEPSEEK_BASE = os.environ.get("DEEPSEEK_BASE", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
BEDROCK_MODEL = os.environ.get("BEDROCK_MODEL", "openai.gpt-oss-120b-1:0")
BEDROCK_REGION = os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION")
_BEDROCK_CLIENT = None


def use_deepseek() -> bool:
    """Use DeepSeek when a key is set and LLM_ENABLED is not false; LLM_ENABLED=false forces local. Checked live on each call (respects runtime env changes)."""
    if os.environ.get("LLM_ENABLED", "true").lower() == "false":
        return False
    return bool(os.environ.get("DEEPSEEK_API_KEY"))


def _backend() -> str:
    """Pick backend: explicit LLM_BACKEND (bedrock/deepseek/ollama) wins, else auto by key (original logic)."""
    b = os.environ.get("LLM_BACKEND", "").strip().lower()
    if b in ("bedrock", "deepseek", "ollama"):
        return b
    return "deepseek" if use_deepseek() else "ollama"


def backend_name() -> str:
    return _backend()


def call(messages: list[dict], *, json_mode: bool = False, temperature: float = 0.0) -> str:
    """Unified chat interface. messages=[{role,content},...]. json_mode=True forces a JSON string return.
    Returns the model output content string. Backend is chosen by _backend()."""
    b = _backend()
    if b == "bedrock":
        return _bedrock(messages, json_mode, temperature)
    if b == "deepseek":
        return _deepseek(messages, json_mode, temperature)
    return _ollama(messages, json_mode, temperature)


def _ollama(messages: list[dict], json_mode: bool, temperature: float) -> str:
    body = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature},
    }
    if json_mode:
        body["format"] = "json"
    r = requests.post(f"{OLLAMA}/api/chat", json=body, timeout=120)
    r.raise_for_status()
    return r.json()["message"]["content"]


def _deepseek(messages: list[dict], json_mode: bool, temperature: float) -> str:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("use_deepseek() 为真但缺 DEEPSEEK_API_KEY")
    body = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "stream": False,
        "temperature": temperature,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    r = requests.post(f"{DEEPSEEK_BASE}/chat/completions", json=body,
                      headers={"Authorization": f"Bearer {key}"}, timeout=120)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _bedrock_client():
    """Lazily build the bedrock-runtime client (non-AWS environments don't need boto3 to import llm)."""
    global _BEDROCK_CLIENT
    if _BEDROCK_CLIENT is None:
        import boto3
        _BEDROCK_CLIENT = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
    return _BEDROCK_CLIENT


# gpt-oss puts its reasoning inside <reasoning>...</reasoning> in content (the answer comes after):
# plain answers strip those blocks; json_mode then robustly extracts the first parseable {..}
# (gpt-oss occasionally emits an extra leading { in json_object mode).
_REASONING_RE = re.compile(r"<reasoning>.*?</reasoning>", re.S)


def _strip_reasoning(text: str) -> str:
    return _REASONING_RE.sub("", text).strip()


def _extract_json(text: str) -> str:
    """After stripping reasoning, scan for the first {..} that raw_decode parses and return normalized JSON;
    if none, return text as-is (let the caller's json.loads fail loudly instead of swallowing)."""
    text = _strip_reasoning(text)
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = dec.raw_decode(text[i:])
                return json.dumps(obj, ensure_ascii=False)
            except json.JSONDecodeError:
                continue
    return text


def _bedrock(messages: list[dict], json_mode: bool, temperature: float) -> str:
    body = {"messages": messages, "temperature": temperature}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    resp = _bedrock_client().invoke_model(modelId=BEDROCK_MODEL, body=json.dumps(body))
    data = json.loads(resp["body"].read())
    content = data["choices"][0]["message"]["content"]
    return _extract_json(content) if json_mode else _strip_reasoning(content)


def call_stream(messages: list[dict], *, temperature: float = 0.0) -> Iterator[str]:
    """Streaming chat: yields content deltas token by token (answer generation only, no json_mode).
    Backend is chosen by _backend(); the caller joins the full text and applies the closing guard."""
    b = _backend()
    if b == "bedrock":
        yield from _bedrock_stream(messages, temperature)
    elif b == "deepseek":
        yield from _deepseek_stream(messages, temperature)
    else:
        yield from _ollama_stream(messages, temperature)


def _ollama_stream(messages: list[dict], temperature: float) -> Iterator[str]:
    body = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": True,
        "options": {"temperature": temperature},
    }
    with requests.post(f"{OLLAMA}/api/chat", json=body, timeout=300, stream=True) as r:
        r.raise_for_status()
        for line in r.iter_lines(decode_unicode=True):
            if not line:
                continue
            obj = json.loads(line)                 # Ollama /api/chat streaming is line-by-line JSON
            piece = obj.get("message", {}).get("content")
            if piece:
                yield piece
            if obj.get("done"):
                break


def _deepseek_stream(messages: list[dict], temperature: float) -> Iterator[str]:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("use_deepseek() 为真但缺 DEEPSEEK_API_KEY")
    body = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "stream": True,
        "temperature": temperature,
    }
    with requests.post(f"{DEEPSEEK_BASE}/chat/completions", json=body,
                       headers={"Authorization": f"Bearer {key}"},
                       timeout=300, stream=True) as r:
        r.raise_for_status()
        for line in r.iter_lines(decode_unicode=True):      # SSE: each line data: {...}
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            piece = json.loads(data)["choices"][0].get("delta", {}).get("content")
            if piece:
                yield piece


_REASON_OPEN = "<reasoning>"
_REASON_CLOSE = "</reasoning>"


def _tail_prefix_len(s: str, tag: str) -> int:
    """How much of s's tail is a prefix of tag (hold back a tag split across chunks)."""
    for k in range(min(len(s), len(tag) - 1), 0, -1):
        if s[-k:] == tag[:k]:
            return k
    return 0


def _drop_reasoning_stream(pieces: Iterator[str]) -> Iterator[str]:
    """Strip ALL <reasoning>...</reasoning> spans from the stream (gpt-oss streams several blocks,
    possibly split across chunks); only emit text outside reasoning. Hold a trailing partial tag;
    never drop real content outside reasoning."""
    buf = ""
    inside = False
    for piece in pieces:
        buf += piece
        out: list[str] = []
        while True:
            if not inside:
                i = buf.find(_REASON_OPEN)
                if i == -1:
                    keep = _tail_prefix_len(buf, _REASON_OPEN)
                    out.append(buf[:len(buf) - keep] if keep else buf)
                    buf = buf[len(buf) - keep:] if keep else ""
                    break
                out.append(buf[:i])
                buf = buf[i + len(_REASON_OPEN):]
                inside = True
            else:
                j = buf.find(_REASON_CLOSE)
                if j == -1:
                    keep = _tail_prefix_len(buf, _REASON_CLOSE)
                    buf = buf[len(buf) - keep:] if keep else ""
                    break
                buf = buf[j + len(_REASON_CLOSE):]
                inside = False
        text = "".join(out)
        if text:
            yield text
    if not inside and buf:
        yield buf              # flush leftover outside reasoning (drop any unclosed reasoning tail)


def _bedrock_stream(messages: list[dict], temperature: float) -> Iterator[str]:
    body = {"messages": messages, "temperature": temperature, "stream": True}
    resp = _bedrock_client().invoke_model_with_response_stream(
        modelId=BEDROCK_MODEL, body=json.dumps(body))

    def _pieces() -> Iterator[str]:
        for event in resp["body"]:
            chunk = event.get("chunk")
            if not chunk:
                continue
            choices = json.loads(chunk["bytes"]).get("choices") or []
            if not choices:
                continue
            piece = (choices[0].get("delta") or {}).get("content")
            if piece:
                yield piece

    yield from _drop_reasoning_stream(_pieces())
