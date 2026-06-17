"""
reranker.py — optional cross-encoder rerank (off by default, kept only as an architecture slot)
(maps to .claude/plans/kb-answerability.md P1)

Lazy load + in-process singleton + graceful fallback: if `KB_RERANK` is not set, torch is never imported, so behavior is the same as no rerank;
if it is set but import fails / OOM / scoring errors, fall back to the bi-encoder original order (only print the reason, never raise and break QA).

Boundary (student-facing red lines + findings conclusion): the reranker **only changes chunk order/selection**,
it **never takes part in refuse decisions** — refusal belongs to P0 (answerability gate + kb_search's bi-encoder min_sim).
Findings show: the reranker fixes recall, not refusal (Mars 0.951 still scores higher than real questions), so it is off by default, not enabled on a 16GB local machine,
and not in the student-facing main path; see docs/rerank_answerability_findings.md.

Security note: the default model jina-reranker-v2 ships its own modeling code, loaded with trust_remote_code=True
(i.e. it runs HuggingFace remote code). Before enabling KB_RERANK, confirm you trust the source, or switch to a model that needs no remote code
(such as BAAI/bge-reranker-v2-m3). This risk does not exist when it is off by default.

Usage (off by default; set the env var to try it):
    KB_RERANK=1 [KB_RERANK_MODEL=jinaai/jina-reranker-v2-base-multilingual] uvicorn ...
    from app.services import reranker
    chunks = reranker.rerank(query, chunks)   # returned unchanged when off / on fallback
"""
from __future__ import annotations
import os

DEFAULT_MODEL = "jinaai/jina-reranker-v2-base-multilingual"  # ~1.1GB, fast on CPU, multilingual (findings suggest trying it first)

_MODEL = None          # in-process singleton
_LOAD_FAILED = False   # once load has failed, do not retry (avoid blowing up on every request)


def enabled() -> bool:
    """Whether rerank is on (read env at runtime, off by default — so when not set, even torch is never imported)."""
    return bool(os.environ.get("KB_RERANK"))


def _load():
    """Lazy-load the cross-encoder singleton; on import/load failure return None (fallback signal), do not raise."""
    global _MODEL, _LOAD_FAILED
    if _MODEL is not None or _LOAD_FAILED:
        return _MODEL
    model_name = os.environ.get("KB_RERANK_MODEL", DEFAULT_MODEL)
    try:
        from sentence_transformers import CrossEncoder  # only place that touches torch
        _MODEL = CrossEncoder(model_name, trust_remote_code=True)
        print(f"[reranker] 已加载:{model_name}")
    except Exception as e:
        _LOAD_FAILED = True
        print(f"[reranker] 加载失败,降级回 bi-encoder 原序:{type(e).__name__}: {e}")
        return None
    return _MODEL


def rerank(query: str, candidates: list[dict]) -> list[dict]:
    """Rerank candidates by descending cross-encoder (query, chunk.text) score; return unchanged when off / no candidates / load or scoring fails.

    candidates are chunks that already passed the bi-encoder min_sim (each has text); this function does not add/remove and does not change refusal,
    it only reorders — the returned dicts are the same batch.
    """
    if not enabled() or len(candidates) < 2:
        return candidates
    model = _load()
    if model is None:
        return candidates
    try:
        scores = model.predict([(query, c.get("text") or "") for c in candidates])
    except Exception as e:
        print(f"[reranker] 打分失败,降级回 bi-encoder 原序:{type(e).__name__}: {e}")
        return candidates
    order = sorted(range(len(candidates)), key=lambda i: float(scores[i]), reverse=True)
    return [candidates[i] for i in order]
