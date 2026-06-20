"""ratelimit.py — 烧钱端点的前置闸:Turnstile 人机验证 + 每 IP 限流 + 每日预算熔断。

只给 /api/ask、/api/ask/stream、/api/sim/advise 这三个会调用付费 LLM/embedding 的端点用。
确定性逻辑全部写死在这里(阈值见 config.py),LLM 不参与任何放行决策。

用法(在端点最前面):
    blocked = ratelimit.check(request)      # request: fastapi.Request
    if blocked is not None:
        return blocked                      # 已是带 error 字段的 JSONResponse(429/403)

单实例进程内状态(dict + 锁),多实例需换 Redis。
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
_daily = {"date": "", "count": 0}               # 全局每日付费请求计数(UTC 日)
_MAX_IPS = 10000                                 # 防内存无限增长的粗保护


def client_ip(request: Request) -> str:
    """取真实客户端 IP。Cloudflare 反代后 request.client 是 CF 的,必须读 CF-Connecting-IP。"""
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
    """校验 Turnstile token。网络异常时 fail-open(放行,避免 CF 抖动导致全站不可用;账单仍有限流+熔断兜底);
    仅当 siteverify 明确返回 success=false 时 fail-closed。"""
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
    """固定窗口:同一分钟内同 IP 超过 RL_PER_MIN 则拒绝。RL_PER_MIN<=0 关闭。"""
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
    """全局每日预算熔断:今日付费请求数超过 LLM_DAILY_CAP 则拒绝。LLM_DAILY_CAP<=0 关闭。"""
    if LLM_DAILY_CAP <= 0:
        return True
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    with _lock:
        if _daily["date"] != today:
            _daily["date"], _daily["count"] = today, 0
        _daily["count"] += 1
        return _daily["count"] <= LLM_DAILY_CAP


def check(request: Request) -> JSONResponse | None:
    """三层前置闸:Turnstile -> 每 IP 限流 -> 每日预算熔断。放行返回 None,拦截返回带 error 的 JSONResponse。"""
    ip = client_ip(request)

    if TURNSTILE_SECRET:
        token = request.headers.get("x-turnstile-response", "")
        print(
            "turnstile debug: incoming headers="
            + str(sorted(request.headers.keys()))
            + f" x-len={len(token)}"
            + f" cf-len={len(request.headers.get('cf-turnstile-response', ''))}",
            flush=True,
        )
        if not _verify_turnstile(token, ip):
            return _refuse("verification_required: 需要完成人机验证后再试", 403)

    if not _rate_ok(ip, time.time()):
        return _refuse(f"rate_limited: 操作过于频繁,请稍后再试。{_BUSY}", 429)

    if not _daily_ok(time.time()):
        return _refuse(f"daily_cap: {_BUSY}", 429)

    return None
