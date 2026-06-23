"""deepeval_config.py — assembles the judge LLM for DeepEval (DeepSeek, deepeval 4.x has a built-in DeepSeekModel).

Sits alongside ragas_config.py and shares eval/.env: the judge also goes through DeepSeek with
temperature=0 for stable scoring. The difference is that DeepEval uses its own DeepSeekModel
(via the official DeepSeek endpoint), not wrapped through langchain.
DeepEval's RAG metrics are all LLM-as-judge and use no embedding, so only the judge is assembled here.
If DEEPSEEK_API_KEY is missing, it raises directly and never silently falls back to the default OpenAI backend (rule 19).

Usage: from deepeval_config import build_judge -> judge = build_judge()
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
