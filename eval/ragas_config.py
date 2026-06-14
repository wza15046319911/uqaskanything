"""ragas_config.py — RAGAS 的 judge LLM 与 embedding 后端装配(确定性配置,不含评测逻辑)。

judge LLM:DeepSeek(OpenAI 兼容端点,经 langchain-openai),temperature=0 取最稳定判分。
embedding:本地 Ollama bge-m3,与生产检索同模型,answer_relevancy 的相似度才有可比性。

用法:from ragas_config import build_judge -> llm, emb = build_judge()
缺 DEEPSEEK_API_KEY 直接抛错,绝不静默退化成默认 OpenAI 后端(规则 19)。
"""
from __future__ import annotations

import os

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_ollama import OllamaEmbeddings
from ragas.llms import LangchainLLMWrapper
from ragas.embeddings import LangchainEmbeddingsWrapper

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))


def build_judge():
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        raise RuntimeError("缺 DEEPSEEK_API_KEY:RAGAS judge 必须走 DeepSeek,请填 eval/.env")

    llm = ChatOpenAI(
        model=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        api_key=key,
        base_url=os.environ.get("DEEPSEEK_BASE", "https://api.deepseek.com"),
        temperature=0,
        timeout=300,
    )
    emb = OllamaEmbeddings(
        model=os.environ.get("OLLAMA_EMBED_MODEL", "bge-m3"),
        base_url=os.environ.get("OLLAMA_URL", "http://localhost:11434"),
    )
    return LangchainLLMWrapper(llm), LangchainEmbeddingsWrapper(emb)
