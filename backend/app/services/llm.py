"""llm.py — 可插拔 LLM 后端(planner 路由 + answer 生成共用)。

后端选择(规则简单):
  - 设了 DEEPSEEK_API_KEY  -> 用 DeepSeek(planner + answer 全程)
  - 没设                    -> 本地 Ollama qwen
  - LLM_ENABLED=false       -> 强制本地 Ollama(即使有 key,临时回退用)

.env:模块导入时自动加载同目录 .env(自带极简解析,无需 python-dotenv);
真实环境变量优先,不被 .env 覆盖。把 key 写进 .env 即可,别提交 git。

env:
  DEEPSEEK_API_KEY  DeepSeek key(决定走不走 DeepSeek)
  DEEPSEEK_BASE     默认 https://api.deepseek.com
  DEEPSEEK_MODEL    默认 deepseek-chat
  OLLAMA_URL        默认 http://localhost:11434
  LLM_MODEL         本地模型,默认 qwen2.5-coder:7b
  LLM_ENABLED       默认 true;设 'false' 强制走本地 Ollama(即使有 key 也不走 DeepSeek)
"""
from __future__ import annotations
import os
import json
import pathlib
from collections.abc import Iterator

import requests


def _load_dotenv(name: str = ".env") -> None:
    """极简 .env 加载:KEY=VALUE 每行;# 注释;真实环境变量优先(setdefault,不覆盖)。"""
    p = pathlib.Path(__file__).resolve().parents[2] / name
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):           # 容忍 `export KEY=VALUE` 写法
            line = line[len("export "):]
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("LLM_MODEL", "qwen2.5-coder:7b")
DEEPSEEK_BASE = os.environ.get("DEEPSEEK_BASE", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")


def use_deepseek() -> bool:
    """有 key 且 LLM_ENABLED 不为 false 时用 DeepSeek;LLM_ENABLED=false 强制本地。每次调用实时判断(尊重运行时改 env)。"""
    if os.environ.get("LLM_ENABLED", "true").lower() == "false":
        return False
    return bool(os.environ.get("DEEPSEEK_API_KEY"))


def backend_name() -> str:
    return "deepseek" if use_deepseek() else "ollama"


def call(messages: list[dict], *, json_mode: bool = False, temperature: float = 0.0) -> str:
    """统一对话接口。messages=[{role,content},...]。json_mode=True 时强制返回 JSON 字符串。
    返回模型输出的 content 字符串。后端按 use_deepseek() 选。"""
    if use_deepseek():
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


def call_stream(messages: list[dict], *, temperature: float = 0.0) -> Iterator[str]:
    """流式对话:逐 token yield content 增量(仅答案生成用,不支持 json_mode)。
    后端按 use_deepseek() 选;调用方负责拼接全文与收尾护栏。"""
    if use_deepseek():
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
            obj = json.loads(line)                 # Ollama /api/chat 流式是逐行 JSON
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
        for line in r.iter_lines(decode_unicode=True):      # SSE: 每行 data: {...}
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            piece = json.loads(data)["choices"][0].get("delta", {}).get("content")
            if piece:
                yield piece
