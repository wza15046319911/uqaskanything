"""ragas_config.py — assembles the judge LLM and embedding backend for RAGAS (deterministic config, no eval logic).

judge LLM: DeepSeek (OpenAI-compatible endpoint, via langchain-openai), temperature=0 for the most stable scoring.
embedding: local Ollama bge-m3, the same model as production retrieval, so answer_relevancy's similarity is comparable.

Usage: from ragas_config import build_judge -> llm, emb = build_judge()
If DEEPSEEK_API_KEY is missing, it raises directly and never silently falls back to the default OpenAI backend (rule 19).
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
