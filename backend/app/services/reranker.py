"""
reranker.py — 可选 cross-encoder 重排(默认关,仅留架构位)
(对应 .claude/plans/kb-answerability.md P1)

懒加载 + 进程内单例 + 失败降级:`KB_RERANK` 未设则永不 import torch,行为与无重排完全一致;
设了但导入失败 / OOM / 打分异常,则降级回 bi-encoder 原序(只 print 原因,绝不抛错拖垮问答)。

边界(student-facing 红线 + findings 结论):reranker **只改 chunk 的顺序/取舍**,
**绝不参与拒答判定**——拒答归 P0(answerability 门 + kb_search 的 bi-encoder min_sim)。
findings 实测:reranker 治召回不治拒答(火星 0.951 仍高于真问题),故默认关、16GB 本机不开,
不进 student-facing 主链路;详见 docs/rerank_answerability_findings.md。

安全提示:默认模型 jina-reranker-v2 自带 modeling 代码,加载用 trust_remote_code=True
(即执行 HuggingFace 远程代码)。开启 KB_RERANK 前请确认信任该来源,或换不需远程代码的模型
(如 BAAI/bge-reranker-v2-m3)。默认关时此风险不存在。

用法(默认关;要试时设环境变量):
    KB_RERANK=1 [KB_RERANK_MODEL=jinaai/jina-reranker-v2-base-multilingual] uvicorn ...
    from app.services import reranker
    chunks = reranker.rerank(query, chunks)   # 关/降级时原样返回
"""
from __future__ import annotations
import os

DEFAULT_MODEL = "jinaai/jina-reranker-v2-base-multilingual"  # ~1.1GB,CPU 快、多语言(findings 推荐先试)

_MODEL = None          # 进程内单例
_LOAD_FAILED = False   # 加载失败过就不再重试(避免每次请求都炸一遍)


def enabled() -> bool:
    """是否开启重排(运行时读 env,默认关——保证不设时连 torch 都不会被 import)。"""
    return bool(os.environ.get("KB_RERANK"))


def _load():
    """懒加载 cross-encoder 单例;import/加载失败返回 None(降级信号),不抛错。"""
    global _MODEL, _LOAD_FAILED
    if _MODEL is not None or _LOAD_FAILED:
        return _MODEL
    model_name = os.environ.get("KB_RERANK_MODEL", DEFAULT_MODEL)
    try:
        from sentence_transformers import CrossEncoder  # 仅此处碰 torch
        _MODEL = CrossEncoder(model_name, trust_remote_code=True)
        print(f"[reranker] 已加载:{model_name}")
    except Exception as e:
        _LOAD_FAILED = True
        print(f"[reranker] 加载失败,降级回 bi-encoder 原序:{type(e).__name__}: {e}")
        return None
    return _MODEL


def rerank(query: str, candidates: list[dict]) -> list[dict]:
    """按 cross-encoder (query, chunk.text) 分降序重排候选;关闭/无候选/加载/打分失败时原样返回。

    candidates 是已过 bi-encoder min_sim 的 chunk(每个含 text);此函数不增删、不改拒答,
    只换顺序——返回的仍是同一批 dict。
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
