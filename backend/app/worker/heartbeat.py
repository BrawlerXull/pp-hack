"""Worker heartbeat and the dashboard's "system" block (header pills: AI, worker, PayPal, simulator).

The worker beats from its own task every few seconds, not from the detector loop: one assessment can wait
on Gemini for a minute, and the owner should see "detector slow", not "worker down". last_tick_at and the
last failed pass tell running, slow, stalled and failing apart. A dead or never-started worker means
nothing detects attacks, which is why it is on every page.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import gemini
from app.config import Settings
from app.db.models import SystemStatus

WORKER = "worker"
BEAT_EVERY_S = 5.0
FRESH = timedelta(seconds=15)  # three missed beats: the worker is gone
# A pass normally takes a second or two; one waiting on Gemini can take a minute. Longer than STALLED and
# the detector is not protecting checkout, whatever the cause.
SLOW = timedelta(seconds=30)
STALLED = timedelta(seconds=150)
LIVE_MAX_AGE = timedelta(seconds=60)  # older tiles are not "live"
log = structlog.get_logger()


async def beat(
    session: AsyncSession,
    now: datetime,
    *,
    started_at: datetime,
    last_tick_at: datetime | None,
    live: dict[str, Any] | None,
    last_tick_error_at: datetime | None = None,
    last_tick_error: str | None = None,
) -> None:
    """Upsert the worker's row. Commits. The detector fields are this process's own and always written
    (NULL until its first pass), so a restarted worker never shows the previous process's ticks or tiles.
    The AI fields keep their stored value when this process has nothing new (the last model that answered
    survives a restart)."""
    ai = gemini.last_outcome()
    values: dict[str, Any] = {
        "heartbeat_at": now,
        "started_at": started_at,
        "last_tick_at": last_tick_at,
        "live": live,
        "last_tick_error_at": last_tick_error_at,
        "last_tick_error": last_tick_error[:120] if last_tick_error else None,
    }
    sticky = {
        "last_ai_model": ai["model"],
        "last_ai_at": ai["at"],
        "last_ai_error": ai["error"][:500] if ai["error"] else None,
        "last_ai_error_at": ai["error_at"],
    }
    values |= {k: v for k, v in sticky.items() if v is not None}
    await session.execute(
        insert(SystemStatus)
        .values(component=WORKER, **values)
        .on_conflict_do_update(index_elements=[SystemStatus.component], set_=values)
    )
    await session.commit()


async def run(sessions: Any, started_at: datetime, state: dict[str, Any], stop: asyncio.Event) -> None:
    """Beat until stop is set. state is shared with the detector loop: last_tick_at, live and the last
    failed pass (last_tick_error_at, last_tick_error)."""
    while not stop.is_set():
        try:
            async with sessions() as s:
                await beat(
                    s,
                    datetime.now(UTC),
                    started_at=started_at,
                    last_tick_at=state.get("last_tick_at"),
                    live=state.get("live"),
                    last_tick_error_at=state.get("last_tick_error_at"),
                    last_tick_error=state.get("last_tick_error"),
                )
        except Exception:
            log.exception("worker_heartbeat_failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=BEAT_EVERY_S)
        except TimeoutError:
            pass


def _ai_block(ai: dict[str, Any], row: SystemStatus | None) -> dict[str, Any]:
    """live: Gemini answers (some models may be paused); cooling_down: every model paused until `until`;
    rules_only: no AI until someone acts (no key, key rejected): the scorecard still decides, with template
    wording and no AI second opinion for unsure cases."""
    state = ai.get("state")
    paused_until = [m["until"] for m in ai.get("models", []) if m.get("paused")]
    if state in ("ok", "degraded"):
        mode, until = "live", None
    elif state == "cooling_down" and paused_until and all(paused_until):
        mode, until = "cooling_down", min(paused_until)
    else:
        mode, until = "rules_only", None
    return {
        "mode": mode,
        "until": until,
        "reason": None if mode == "live" and state == "ok" else ai.get("detail"),
        "last_model": row.last_ai_model if row else None,
        "last_answer_at": row.last_ai_at if row else None,
        "last_error": row.last_ai_error if row else None,
        "last_error_at": row.last_ai_error_at if row else None,
    }


def detector_state(row: SystemStatus | None, now: datetime) -> str:
    """stopped: no heartbeat. failing: the last pass raised. slow / stalled: no pass finished for SLOW /
    STALLED (counted from the worker's start when none has finished yet). running otherwise."""
    if row is None or now - row.heartbeat_at >= FRESH:
        return "stopped"
    if row.last_tick_error_at and (row.last_tick_at is None or row.last_tick_error_at > row.last_tick_at):
        return "failing"
    age = now - (row.last_tick_at or row.started_at)
    return "stalled" if age > STALLED else "slow" if age > SLOW else "running"


def _live(row: SystemStatus, now: datetime) -> dict[str, Any] | None:
    try:
        at = datetime.fromisoformat((row.live or {})["at"])
    except (KeyError, TypeError, ValueError):
        return None
    return row.live if row.started_at <= at and now - at < LIVE_MAX_AGE else None


def system_block(
    row: SystemStatus | None,
    ai: dict[str, Any],
    settings: Settings,
    now: datetime,
    *,
    card_capture_seen: bool,
    assessing: bool = False,
) -> dict[str, Any]:
    """The dashboard's system status. card_capture_seen: a real (sandbox) card payment was captured lately,
    the only evidence we have that card processing works on this PayPal account; otherwise "unknown".
    assessing: an incident is waiting on its AI assessment, the only time "waiting on AI" is true."""
    state = detector_state(row, now)
    alive = state != "stopped"
    return {
        "worker": {
            "alive": alive,
            "state": state,
            "assessing": assessing,
            "heartbeat_at": row.heartbeat_at if row else None,
            "last_tick_at": row.last_tick_at if row else None,
            "last_tick_error": row.last_tick_error if row else None,
            "last_tick_error_at": row.last_tick_error_at if row else None,
            "started_at": row.started_at if row else None,
        },
        "ai": _ai_block(ai, row),
        "paypal": {
            "environment": "sandbox",  # config refuses anything else (Settings._sandbox_only)
            "configured": bool(settings.paypal_client_id and settings.paypal_client_secret),
            "card_processing": "seen_working" if card_capture_seen else "unknown",
        },
        "simulator_enabled": settings.simulator_enabled,
        # The detector's last 5/15-minute numbers: only while the worker is alive and they are recent.
        "live": _live(row, now) if alive and row else None,
    }
