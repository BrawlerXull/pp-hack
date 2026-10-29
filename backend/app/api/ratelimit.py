"""Per-client rate limits on the endpoints worth abusing: the owner login (password guessing), order
creation and the precheck (card testing through our own API), the human check, PayPal client tokens (each
one is a PayPal call) and the AI agent (each turn is a Gemini call on the shared quota).

A sliding window per (endpoint, client IP), in this process's memory: the API runs as one process, and a
restart forgetting the counts is harmless. The IP is the one the guard uses (app/guard/signals.py), so a
typed X-Forwarded-For does not buy a fresh allowance. These limits are generous for a person and only stop
floods; the guard's own rules (device and network velocity) still decide what a checkout may do.
"""

import math
import time
from collections import deque
from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request

from app.config import get_settings
from app.guard import signals

MAX_KEYS = 50_000  # past this, idle keys are dropped (a flood of fresh IPs cannot grow memory forever)

_hits: dict[str, deque[float]] = {}


def reset() -> None:
    _hits.clear()


def _hit(key: str, limit: int, window_s: float, now: float, record: bool = True) -> float | None:
    """Records a hit (unless `record` is off); returns the seconds to wait when the window is already full
    (that hit is not counted)."""
    q = _hits.setdefault(key, deque())
    while q and q[0] <= now - window_s:
        q.popleft()
    if len(q) >= limit:
        return q[0] + window_s - now
    if record:
        q.append(now)
    if len(_hits) > MAX_KEYS:
        for k in [k for k, v in _hits.items() if not v or v[-1] <= now - window_s]:
            del _hits[k]
    return None


def _key(name: str, request: Request) -> str:
    return f"{name}:{signals.request_ip(request) or 'unknown'}"


def record(name: str, request: Request) -> None:
    """Counts one hit for a limit declared with count_all=False (e.g. a failed login only)."""
    _hits.setdefault(_key(name, request), deque()).append(time.monotonic())


def limit(
    name: str, times: int, per_s: float, count_all: bool = True
) -> Callable[[Request], Awaitable[None]]:
    """FastAPI dependency: at most `times` requests per `per_s` seconds per client IP on this endpoint.
    With count_all=False it only refuses once full; the endpoint counts what matters with record()."""

    async def check(request: Request) -> None:
        if not get_settings().rate_limits_enabled:
            return
        wait = _hit(_key(name, request), times, per_s, time.monotonic(), record=count_all)
        if wait is not None:
            seconds = max(1, math.ceil(wait))
            raise HTTPException(
                429,
                {"code": "rate_limited", "message": f"Too many requests: try again in {seconds} s"},
                headers={"Retry-After": str(seconds)},
            )

    return check
