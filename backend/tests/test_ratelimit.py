"""ratelimit 前置闸单测:真实 IP 提取 + 每 IP 固定窗口 + 每日熔断 + Turnstile 开关。纯逻辑,无需 DB。"""
from types import SimpleNamespace

import pytest

from app.core import ratelimit


def _req(headers=None, host="1.2.3.4"):
    return SimpleNamespace(headers=(headers or {}), client=SimpleNamespace(host=host))


@pytest.fixture(autouse=True)
def _reset():
    ratelimit._ip_window.clear()
    ratelimit._daily.update(date="", count=0)
    yield


def test_client_ip_prefers_cf_then_xff_then_peer():
    assert ratelimit.client_ip(_req({"cf-connecting-ip": "9.9.9.9", "x-forwarded-for": "8.8.8.8"})) == "9.9.9.9"
    assert ratelimit.client_ip(_req({"x-forwarded-for": "8.8.8.8, 7.7.7.7"})) == "8.8.8.8"
    assert ratelimit.client_ip(_req(host="5.5.5.5")) == "5.5.5.5"


def test_rate_limit_blocks_after_cap(monkeypatch):
    monkeypatch.setattr(ratelimit, "RL_PER_MIN", 3)
    now = 1_000_000.0
    assert [ratelimit._rate_ok("ip", now) for _ in range(4)] == [True, True, True, False]
    # 不同 IP 各自独立计数
    assert ratelimit._rate_ok("other", now) is True


def test_rate_limit_resets_next_minute(monkeypatch):
    monkeypatch.setattr(ratelimit, "RL_PER_MIN", 1)
    assert ratelimit._rate_ok("ip", 60.0) is True
    assert ratelimit._rate_ok("ip", 60.0) is False
    assert ratelimit._rate_ok("ip", 120.0) is True            # 下一分钟窗口重置


def test_daily_cap_blocks_then_resets_next_day(monkeypatch):
    monkeypatch.setattr(ratelimit, "LLM_DAILY_CAP", 2)
    day = 1_000_000.0                                          # 1970-01-12
    assert [ratelimit._daily_ok(day) for _ in range(3)] == [True, True, False]
    assert ratelimit._daily_ok(day + 86_400) is True          # 跨 UTC 日重置


def test_check_passes_when_under_limits(monkeypatch):
    monkeypatch.setattr(ratelimit, "TURNSTILE_SECRET", "")
    monkeypatch.setattr(ratelimit, "RL_PER_MIN", 100)
    monkeypatch.setattr(ratelimit, "LLM_DAILY_CAP", 100)
    assert ratelimit.check(_req()) is None


def test_check_returns_429_when_rate_limited(monkeypatch):
    monkeypatch.setattr(ratelimit, "TURNSTILE_SECRET", "")
    monkeypatch.setattr(ratelimit, "RL_PER_MIN", 1)
    monkeypatch.setattr(ratelimit, "LLM_DAILY_CAP", 100)
    req = _req()
    assert ratelimit.check(req) is None
    blocked = ratelimit.check(req)
    assert blocked is not None and blocked.status_code == 429


def test_check_blocks_403_when_turnstile_required_and_token_missing(monkeypatch):
    monkeypatch.setattr(ratelimit, "TURNSTILE_SECRET", "secret")
    blocked = ratelimit.check(_req())                         # 无 cf-turnstile-response 头
    assert blocked is not None and blocked.status_code == 403


def test_turnstile_fail_open_on_network_error(monkeypatch):
    import requests

    def boom(*a, **k):
        raise requests.RequestException("down")

    monkeypatch.setattr(ratelimit.requests, "post", boom)
    assert ratelimit._verify_turnstile("tok", "1.2.3.4") is True
