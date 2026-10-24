"""Gemini client wrapper: structured JSON output, timeout, a fallback chain and a per-model circuit breaker.

Callers get `(parsed_model, model_id)` or an `AIUnavailable` they must handle with a deterministic fallback.
Models paused by the breaker (app/ai/breaker.py) are skipped; when all are paused no call is made at all.
Every call made inside `track_usage()` is recorded with its token counts, so callers can audit the cost.
"""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

import structlog
from google import genai
from google.genai import types
from pydantic import BaseModel

from app.ai import breaker as brk
from app.config import Settings, get_settings
from app.db.models import CooldownReason

log = structlog.get_logger()


class AIUnavailable(RuntimeError):
    def __init__(self, message: str, reason: str | None = None):
        super().__init__(message)
        self.reason = reason  # short owner-facing text, e.g. "Gemini quota exhausted"


_breaker = brk.Breaker()
store: brk.BreakerStore = brk.DbStore()
_usage: ContextVar[list[dict[str, Any]] | None] = ContextVar("gemini_usage", default=None)
# This process's last Gemini answer and last failure, for the worker heartbeat (dashboard AI pill).
_last: dict[str, Any] = {"model": None, "at": None, "error": None, "error_at": None}


def last_outcome() -> dict[str, Any]:
    return dict(_last)


@contextmanager
def track_usage() -> Iterator[list[dict[str, Any]]]:
    """Collects one record per Gemini request made inside the block (failed ones too: they cost quota)."""
    calls: list[dict[str, Any]] = []
    token = _usage.set(calls)
    try:
        yield calls
    finally:
        _usage.reset(token)


def _record_usage(model: str, res: Any = None, error: str | None = None) -> None:
    meta = getattr(res, "usage_metadata", None)
    entry = {
        "model": getattr(res, "model_version", None) or model,
        "ok": error is None,
        "prompt_tokens": getattr(meta, "prompt_token_count", None),
        "output_tokens": getattr(meta, "candidates_token_count", None),
        "thinking_tokens": getattr(meta, "thoughts_token_count", None),
        "total_tokens": getattr(meta, "total_token_count", None),
    }
    if error is not None:
        entry["error"] = error[:120]
    log.info("gemini_usage", **entry)
    if (calls := _usage.get()) is not None:
        calls.append(entry)


def _now() -> datetime:
    return datetime.now(UTC)


@lru_cache
def _client() -> genai.Client:
    key = get_settings().gemini_api_key
    if not key:
        raise AIUnavailable("GEMINI_API_KEY not set", "no Gemini API key configured")
    return genai.Client(api_key=key)


def _thinking(model: str) -> types.ThinkingConfig | None:
    # Light reasoning keeps verdicts fast (seconds, not tens of seconds) without hurting accuracy
    # on this task (scripts/eval_ai.py).
    if model.startswith("gemini-2.5"):
        return types.ThinkingConfig(thinking_budget=512)
    if model.startswith("gemini-3"):
        return types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW)
    return None


def models(settings: Settings | None = None) -> list[str]:
    settings = settings or get_settings()
    chain = [settings.gemini_model, *settings.gemini_fallback_models.split(",")]
    return list(dict.fromkeys(m.strip() for m in chain if m.strip()))  # dedupe, keep order


async def cooling_down() -> bool:
    """True when every model is paused, per the shared table (one SELECT). Re-reading it matters in the API
    process: a stale in-memory "key rejected" (until restart) would otherwise outlive the worker's reset."""
    key = get_settings().gemini_api_key
    if key:
        await _sync_from_store(key)
    return not _breaker.usable(models(), _now())


def _reason(cds: list[brk.Cooldown]) -> str:
    reasons = {cd.reason for cd in cds}
    for r in (CooldownReason.BAD_KEY, CooldownReason.DAILY_QUOTA, CooldownReason.QUOTA):
        if r in reasons:
            return brk.FRIENDLY[r]
    return brk.FRIENDLY[next(iter(reasons))] if reasons else "Gemini request failed"


async def _sync_from_store(key: str) -> None:
    shared = await store.load(key)
    if shared is not None:
        _breaker.state = shared


async def status() -> dict[str, Any]:
    """Breaker state for /healthz and the dashboard, read from the shared table (worker + API agree)."""
    settings = get_settings()
    if not settings.gemini_api_key:
        return {"state": "not_configured", "detail": "missing GEMINI_API_KEY", "models": []}
    await _sync_from_store(settings.gemini_api_key)
    now, chain = _now(), models(settings)
    paused = {m: cd for m in chain if (cd := _breaker.blocked(m, now))}
    rows = [
        {
            "model": m,
            "paused": m in paused,
            "reason": paused[m].reason if m in paused else None,
            "until": paused[m].until.isoformat() if m in paused and paused[m].until else None,
        }
        for m in chain
    ]
    if len(paused) < len(chain):
        state = "ok" if not paused else "degraded"
        detail = "configured" if not paused else f"configured; paused {_breaker.describe(chain, now)}"
    else:
        key_bad = any(cd.reason == CooldownReason.BAD_KEY for cd in paused.values())
        state = "key_rejected" if key_bad else "cooling_down"
        detail = f"{_reason(list(paused.values()))}: {_breaker.describe(chain, now)}"
    return {"state": state, "detail": detail, "models": rows}


async def generate_json[T: BaseModel](
    *, system: str, prompt: str, schema: type[T], temperature: float = 0.1
) -> tuple[T, str]:
    settings = get_settings()
    key = settings.gemini_api_key
    if not key:
        raise AIUnavailable("GEMINI_API_KEY not set", "no Gemini API key configured")
    chain = models(settings)
    await _sync_from_store(key)
    now = _now()
    usable = _breaker.usable(chain, now)
    if not usable:
        cds = [cd for m in chain if (cd := _breaker.blocked(m, now))]
        raise AIUnavailable(f"all Gemini models cooling down: {_breaker.describe(chain, now)}", _reason(cds))

    last_error: Exception | None = None
    tripped: list[brk.Cooldown] = []
    for model in usable:
        res = None
        try:
            res = await asyncio.wait_for(
                _client().aio.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system,
                        response_mime_type="application/json",
                        response_schema=schema,
                        temperature=temperature,
                        thinking_config=_thinking(model),
                    ),
                ),
                timeout=settings.ai_timeout_s,
            )
            _record_usage(model, res)  # billed even if the JSON below turns out unusable
            parsed = (
                res.parsed if isinstance(res.parsed, schema) else schema.model_validate_json(res.text or "")
            )
            if _breaker.clear(model):
                await store.clear(key, model)
            _last.update(model=res.model_version or model, at=_now())
            return parsed, res.model_version or model
        except AIUnavailable:
            raise
        except Exception as exc:  # timeout, quota, bad JSON…: log, maybe pause the model, try the next one
            last_error = exc
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
            if res is None:  # no response at all (timeout, quota, 5xx); a bad-JSON reply is already recorded
                _record_usage(model, error=error)
            trip = brk.classify(exc)
            # Shown on the dashboard: the classified reason only, never the raw SDK text (URLs, project ids).
            shown = brk.FRIENDLY[trip.reason] if trip else f"unusable answer ({type(exc).__name__})"
            _last.update(error=f"{model}: {shown}", error_at=_now())
            log.warning("gemini_call_failed", model=model, error=error, pause=trip and trip.reason)
            if trip is None:
                continue
            name, cd = _breaker.trip(model, trip, _now(), error)
            tripped.append(cd)
            await store.save(key, name, cd)
            if trip.all_models:  # same key for every model: trying the rest only burns time
                raise AIUnavailable(f"Gemini API key rejected: {error}", _reason([cd])) from exc
    raise AIUnavailable(
        f"all Gemini models failed: {last_error}", _reason(tripped) if tripped else None
    ) from last_error
