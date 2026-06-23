"""ratelimit.py — front gate for the costly endpoints: Turnstile human check + per-IP rate limit + daily budget cut-off.

Used only by /api/ask, /api/ask/stream, /api/sim/advise — the three endpoints that call the paid LLM/embedding.
All deterministic logic is hard-coded here (thresholds in config.py); the LLM takes no part in any pass/block decision.

Usage (at the very start of an endpoint):
    blocked = ratelimit.check(request)      # request: fastapi.Request
    if blocked is not None:
        return blocked                      # already a JSONResponse with an error field (429/403)

State lives in the process (dict + lock) for a single instance; multiple instances need Redis instead.
"""
from __future__ import annotations

import threading
import time

import requests
from fastapi import Request
from fastapi.responses import JSONResponse

from app.core.config import LLM_DAILY_CAP, RL_PER_MIN, TURNSTILE_SECRET

_OFFICIAL = "https://my.uq.edu.au/programs-courses/"
_BUSY = f"当前访问量较大,请稍后再试。你也可以直接查询 UQ 官方课程库:{_OFFICIAL}"

_lock = threading.Lock()
_ip_window: dict[str, tuple[int, int]] = {}     # ip -> (window_start_minute, count)
_daily = {"date": "", "count": 0}               # global daily paid-request count (UTC day)
_MAX_IPS = 10000                                 # rough guard against unbounded memory growth


def client_ip(request: Request) -> str:
    """Get the real client IP. Behind the Cloudflare proxy request.client is CF's, so read CF-Connecting-IP."""
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _refuse(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _verify_turnstile(token: str, ip: str) -> bool:
    """Check the Turnstile token. On a network error fail-open (let it pass, so a CF wobble does not take the whole
    site down; the bill is still covered by the rate limit + daily cut-off); fail-closed only when siteverify
    clearly returns success=false."""
    try:
        r = requests.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data={"secret": TURNSTILE_SECRET, "response": token, "remoteip": ip},
            timeout=5,
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            print(f"turnstile siteverify failed: {data.get('error-codes')}", flush=True)
        return bool(data.get("success"))
    except requests.RequestException:
        return True


def _rate_ok(ip: str, now: float) -> bool:
    """Fixed window: reject if the same IP goes over RL_PER_MIN within one minute. RL_PER_MIN<=0 turns it off."""
    if RL_PER_MIN <= 0:
        return True
    minute = int(now // 60)
    with _lock:
        if len(_ip_window) > _MAX_IPS:
            _ip_window.clear()
        start, count = _ip_window.get(ip, (minute, 0))
        if start != minute:
            start, count = minute, 0
        count += 1
        _ip_window[ip] = (start, count)
        return count <= RL_PER_MIN


def _daily_ok(now: float) -> bool:
    """Global daily budget cut-off: reject when today's paid requests go over LLM_DAILY_CAP. LLM_DAILY_CAP<=0 turns it off."""
    if LLM_DAILY_CAP <= 0:
        return True
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    with _lock:
        if _daily["date"] != today:
            _daily["date"], _daily["count"] = today, 0
        _daily["count"] += 1
        return _daily["count"] <= LLM_DAILY_CAP


def check(request: Request) -> JSONResponse | None:
    """Three-layer front gate: Turnstile -> per-IP rate limit -> daily budget cut-off. Returns None to pass, or a JSONResponse with an error to block."""
    ip = client_ip(request)

    if TURNSTILE_SECRET:
        token = request.headers.get("x-turnstile-response", "")
        if not _verify_turnstile(token, ip):
            return _refuse("verification_required: 需要完成人机验证后再试", 403)

    if not _rate_ok(ip, time.time()):
        return _refuse(f"rate_limited: 操作过于频繁,请稍后再试。{_BUSY}", 429)

    if not _daily_ok(time.time()):
        return _refuse(f"daily_cap: {_BUSY}", 429)

    return None
