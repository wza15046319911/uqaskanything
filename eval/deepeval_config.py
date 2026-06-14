"""deepeval_config.py — DeepEval 的 judge LLM 装配(DeepSeek,deepeval 4.x 内置 DeepSeekModel)。

与 ragas_config.py 并列、共用 eval/.env:judge 同样走 DeepSeek、temperature=0 取稳定判分,
区别是 DeepEval 用自带的 DeepSeekModel(走 DeepSeek 官方端点),不经 langchain 包装。
DeepEval 的 RAG 指标全是 LLM-as-judge,不用 embedding,所以这里只装配 judge。
缺 DEEPSEEK_API_KEY 直接抛错,绝不静默退化成默认 OpenAI 后端(规则 19)。

用法:from deepeval_config import build_judge -> judge = build_judge()
"""
from __future__ import annotations

import os

from dotenv import load_dotenv
from deepeval.models import DeepSeekModel

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))


def build_judge() -> DeepSeekModel:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        raise RuntimeError("缺 DEEPSEEK_API_KEY:DeepEval judge 必须走 DeepSeek,请填 eval/.env")
    return DeepSeekModel(
        model=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        api_key=key,
        temperature=0,
    )
