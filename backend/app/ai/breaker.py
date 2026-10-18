"""Per-model circuit breaker for Gemini.

Without it every assessment walks the whole fallback chain against an exhausted or rejected key: about six
failing calls per tick (analyst + reporter), each up to the timeout. A paused model is skipped until its
cooldown ends; when every model is paused callers get AIUnavailable at once and use their deterministic
fallback. The state lives in ai_model_state so the API process (close/regenerate, /healthz) and the worker
see the same truth; every check re-reads it, and the in-process copy is only a fallback when the DB is down.
"""

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx
import structlog
from google.genai import errors as genai_errors
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from app.db.models import AIModelState, CooldownReason
from app.db.session import get_sessionmaker

log = structlog.get_logger()

ALL_MODELS = "*"  # row/entry that pauses every model (the key was rejected)

QUOTA_DEFAULT_S = 60.0
DAILY_QUOTA_S = 3600.0  # re-probe hourly: per-day quotas reset at an unknown (Pacific) midnight
OVERLOADED_S = 20.0
TIMEOUT_S = 15.0
BAD_REQUEST_S = 600.0
MAX_DELAY_S = 24 * 3600.0

FRIENDLY = {
    CooldownReason.QUOTA: "Gemini quota exhausted",
    CooldownReason.DAILY_QUOTA: "Gemini daily quota exhausted",
    CooldownReason.OVERLOADED: "Gemini temporarily overloaded",
    CooldownReason.TIMEOUT: "Gemini timed out",
    CooldownReason.BAD_REQUEST: "Gemini rejected the request",
    CooldownReason.BAD_KEY: "Gemini API key rejected",
}


@dataclass(frozen=True)
class Trip:
    """What one failure means for the breaker. seconds=None: paused until restart."""

    reason: CooldownReason
    seconds: float | None
    all_models: bool = False


@dataclass
class Cooldown:
    reason: CooldownReason
    until: datetime | None  # None = until restart
    failures: int = 1
    last_error: str = ""

    def active(self, now: datetime) -> bool:
        return self.until is None or self.until > now


def key_id(api_key: str) -> str:
    """Stable, non-reversible id of the API key: state learned with an old key never blocks a new one."""
    return hashlib.sha256(api_key.encode()).hexdigest()[:16]


def _walk(obj: Any) -> Any:
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def retry_delay(details: Any) -> float | None:
    """google.rpc.RetryInfo.retryDelay ("37s", "1.5s") anywhere in the error body, in seconds."""
    for d in _walk(details):
        raw = d.get("retryDelay")
        if isinstance(raw, str) and (m := re.fullmatch(r"\s*(\d+(?:\.\d+)?)s\s*", raw)):
            return min(max(float(m.group(1)), 1.0), MAX_DELAY_S)
    return None


_PER_DAY = re.compile(r"per\s*day|perday|daily", re.IGNORECASE)
# Names the key or the key's project, not one model: API_KEY_INVALID, "API key not valid",
# API_KEY_SERVICE_BLOCKED, SERVICE_DISABLED (Generative Language API off for the project).
_KEY_PROBLEM = re.compile(r"api[\s_]?key|SERVICE_DISABLED", re.IGNORECASE)


def classify(exc: BaseException) -> Trip | None:
    """Map a failed call to a cooldown. None = the model is up but answered badly (bad JSON): no pause."""
    if isinstance(exc, TimeoutError | httpx.TimeoutException | httpx.TransportError):
        return Trip(CooldownReason.TIMEOUT, TIMEOUT_S)
    if not isinstance(exc, genai_errors.APIError):
        return None
    code, status = exc.code or 0, exc.status or ""
    text = f"{exc.message or ''} {exc.details!s}"
    if code == 429 or status == "RESOURCE_EXHAUSTED":
        delay = retry_delay(exc.details)
        if _PER_DAY.search(text):
            return Trip(CooldownReason.DAILY_QUOTA, max(delay or 0.0, DAILY_QUOTA_S))
        return Trip(CooldownReason.QUOTA, delay or QUOTA_DEFAULT_S)
    # Gemini answers a bad key with 400 INVALID_ARGUMENT "API key not valid". Other 400s (a config one
    # model doesn't support) and 403s (a preview model the project isn't allowlisted for) are that model's
    # problem and must not take the working fallbacks down until a restart.
    if code == 401 or (code in (400, 403) and _KEY_PROBLEM.search(text)):
        return Trip(CooldownReason.BAD_KEY, None, all_models=True)
    if code >= 500:
        return Trip(CooldownReason.OVERLOADED, OVERLOADED_S)
    if 400 <= code < 500:
        return Trip(CooldownReason.BAD_REQUEST, BAD_REQUEST_S)
    return None


class Breaker:
    """In-process view of the cooldowns (pure: no I/O, `now` passed in)."""

    def __init__(self) -> None:
        self.state: dict[str, Cooldown] = {}

    def blocked(self, model: str, now: datetime) -> Cooldown | None:
        for name in (ALL_MODELS, model):
            cd = self.state.get(name)
            if cd is not None and cd.active(now):
                return cd
        return None

    def usable(self, models: list[str], now: datetime) -> list[str]:
        return [m for m in models if self.blocked(m, now) is None]

    def trip(self, model: str, trip: Trip, now: datetime, error: str = "") -> tuple[str, Cooldown]:
        name = ALL_MODELS if trip.all_models else model
        prev = self.state.get(name)
        until = None if trip.seconds is None else now + timedelta(seconds=trip.seconds)
        cd = Cooldown(trip.reason, until, (prev.failures + 1) if prev else 1, error[:500])
        self.state[name] = cd
        return name, cd

    def clear(self, model: str) -> bool:
        return self.state.pop(model, None) is not None

    def describe(self, models: list[str], now: datetime) -> str:
        parts = []
        for m in models:
            if cd := self.blocked(m, now):
                left = "until restart" if cd.until is None else f"{(cd.until - now).total_seconds():.0f}s"
                parts.append(f"{m}: {cd.reason} ({left})")
        return ", ".join(parts)


class BreakerStore(Protocol):
    async def load(self, key: str) -> dict[str, Cooldown] | None: ...
    async def save(self, key: str, model: str, cd: Cooldown) -> None: ...
    async def clear(self, key: str, model: str) -> None: ...


class MemoryStore:
    """Process-local store (tests, scripts). Returning None keeps the breaker's own copy."""

    async def load(self, key: str) -> dict[str, Cooldown] | None:
        return None

    async def save(self, key: str, model: str, cd: Cooldown) -> None:
        return None

    async def clear(self, key: str, model: str) -> None:
        return None


class DbStore:
    """ai_model_state via short sessions of their own, independent of the caller's transaction (which may
    roll back). A DB hiccup only costs the shared view: it is logged and the in-process copy is kept."""

    async def load(self, key: str) -> dict[str, Cooldown] | None:
        # Expired rows are loaded too (one per model at most): a later success then deletes them, and a
        # repeat failure counts as consecutive.
        try:
            async with get_sessionmaker()() as s:
                rows = (
                    await s.execute(select(AIModelState).where(AIModelState.key_id == key_id(key)))
                ).scalars()
                return {
                    r.model: Cooldown(CooldownReason(r.reason), r.until, r.failures, r.last_error or "")
                    for r in rows
                }
        except Exception as exc:
            log.warning("ai_breaker_load_failed", error=f"{type(exc).__name__}: {str(exc)[:200]}")
            return None

    async def save(self, key: str, model: str, cd: Cooldown) -> None:
        values = dict(
            model=model,
            key_id=key_id(key),
            reason=cd.reason,
            until=cd.until,
            failures=cd.failures,
            last_error=cd.last_error,
            updated_at=datetime.now(UTC),
        )
        stmt = insert(AIModelState).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[AIModelState.model], set_={k: v for k, v in values.items() if k != "model"}
        )
        try:
            async with get_sessionmaker()() as s:
                await s.execute(stmt)
                await s.commit()
        except Exception as exc:
            log.warning("ai_breaker_save_failed", error=f"{type(exc).__name__}: {str(exc)[:200]}")

    async def clear(self, key: str, model: str) -> None:
        try:
            async with get_sessionmaker()() as s:
                await s.execute(
                    delete(AIModelState).where(
                        AIModelState.model == model, AIModelState.key_id == key_id(key)
                    )
                )
                await s.commit()
        except Exception as exc:
            log.warning("ai_breaker_clear_failed", error=f"{type(exc).__name__}: {str(exc)[:200]}")


async def reset_until_restart() -> None:
    """Process start: drop "until restart" pauses (a rejected key) so a fixed key is tried again."""
    try:
        async with get_sessionmaker()() as s:
            await s.execute(delete(AIModelState).where(AIModelState.until.is_(None)))
            await s.commit()
    except Exception as exc:
        log.warning("ai_breaker_reset_failed", error=f"{type(exc).__name__}: {str(exc)[:200]}")
