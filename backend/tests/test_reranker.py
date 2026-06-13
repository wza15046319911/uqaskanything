"""可选 reranker 骨架:默认关=零行为改变 + 不碰 torch;开启时只改顺序、失败降级。无 DB / 无 torch。"""
import os
import sys
import subprocess

from app.core.config import BACKEND_ROOT
from app.services import reranker


def test_enabled_reads_env_live(monkeypatch):
    monkeypatch.delenv("KB_RERANK", raising=False)
    assert reranker.enabled() is False
    monkeypatch.setenv("KB_RERANK", "1")
    assert reranker.enabled() is True


def test_rerank_disabled_is_identity(monkeypatch):
    monkeypatch.delenv("KB_RERANK", raising=False)
    cands = [{"text": "a"}, {"text": "b"}]
    assert reranker.rerank("q", cands) is cands     # 原样同一对象返回 -> 行为与无重排完全一致


def test_rerank_short_list_not_loaded(monkeypatch):
    # <2 候选不重排,不触发加载(免在只有一条时白白拉模型)
    monkeypatch.setenv("KB_RERANK", "1")
    one = [{"text": "a"}]
    assert reranker.rerank("q", one) is one


def test_rerank_reorders_by_score(monkeypatch):
    monkeypatch.setenv("KB_RERANK", "1")

    class _Fake:
        def predict(self, pairs):
            return [0.1, 0.9, 0.5]      # b 最高 -> c -> a

    monkeypatch.setattr(reranker, "_load", lambda: _Fake())
    out = reranker.rerank("q", [{"text": "a"}, {"text": "b"}, {"text": "c"}])
    assert [c["text"] for c in out] == ["b", "c", "a"]


def test_rerank_degrades_when_model_unavailable(monkeypatch):
    # 加载失败(_load 返回 None)-> 原序返回,不抛错
    monkeypatch.setenv("KB_RERANK", "1")
    monkeypatch.setattr(reranker, "_load", lambda: None)
    cands = [{"text": "a"}, {"text": "b"}]
    assert reranker.rerank("q", cands) is cands


def test_rerank_degrades_on_predict_error(monkeypatch):
    monkeypatch.setenv("KB_RERANK", "1")

    class _Boom:
        def predict(self, pairs):
            raise RuntimeError("oom")

    monkeypatch.setattr(reranker, "_load", lambda: _Boom())
    cands = [{"text": "a"}, {"text": "b"}]
    assert reranker.rerank("q", cands) is cands


def test_import_retrieval_does_not_pull_torch_when_disabled():
    # 红线:不设 KB_RERANK 时,import 检索/重排链路绝不 import torch(懒加载保证)
    code = (
        "import sys, app.services.retrieval, app.services.reranker, app.services.qa\n"
        "assert not app.services.reranker.enabled()\n"
        "bad = [m for m in sys.modules if m == 'torch' or m.startswith('torch.')]\n"
        "assert not bad, bad\n"
        "print('OK')\n"
    )
    env = dict(os.environ)
    env.pop("KB_RERANK", None)
    env["PYTHONPATH"] = str(BACKEND_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    r = subprocess.run([sys.executable, "-c", code], cwd=str(BACKEND_ROOT),
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "OK" in r.stdout
