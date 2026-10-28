"""When to re-assess an incident, when to ask Gemini again, and when to stop trusting a kept tie-break.
Pure functions.

An assessment re-runs the scorecard (free) and asks Gemini to explain or break a tie (quota and seconds).
Most ticks of a contained attack change nothing the verdict depends on (blocked bots keep
`blocked_activity` firing), so an incident is re-assessed only on a material change; otherwise the current
verdict's policy is re-applied to new data (IncidentService._enforce). After Gemini failures, assessments
still happen on material changes but Gemini itself is only retried on a backoff (ai_due).

The scorecard always answers, so "keeping the last verdict" now only concerns Gemini's part: when a
tie-break the AI agreed to cannot be re-confirmed (a 429 mid-attack), the agreed class is kept rather than
dropped to "unclear", with two guards: it yields to new signals that contradict it (fraud signals for a
"harmless" verdict, PayPal errors for an attack verdict), and one that stays unconfirmed for too long stops
moving money without the owner (policy R5, verdict_stale; an agreed tie-break is capped below the refund
floor anyway). A failed explanation keeps the last AI wording for the same class, or falls back to ours.
"""

from datetime import datetime, timedelta
from typing import Any

from app.ai.schemas import ATTACK_CLASSES
from app.db.models import Incident
from app.detection.triggers import FRAUD_TRIGGERS
from app.incidents.decision import DECIDED_BY_AGREE
from app.policy.engine import CONFIDENCE_ACT, CONFIDENCE_REFUND, HARMLESS_CLASSES

RULES_FALLBACK = "rules-fallback"  # AnalystResult.model_id when Gemini was unavailable
TEMPLATE = "template"  # Incident.model_id when the verdict's text is our own wording, not Gemini's
# Triggers that point at PayPal failing: real shoppers retrying through an outage look like a bot that
# retries (3+ attempts per device), so a kept attack verdict must not keep blocking and refunding them.
OUTAGE_TRIGGERS = frozenset({"processor_errors"})

MIN_GAP = timedelta(seconds=30)  # even a material change waits this long after the previous call
VERDICT_MAX_AGE = timedelta(minutes=10)  # a verdict this old is re-asked while signals keep firing
STALE_AFTER_FAILURES = 3
STALE_AFTER = timedelta(minutes=20)
GROWTH = 1.5  # attempts in the last 15 min grew by 50% or more since the last call
MAX_BACKOFF_DOUBLINGS = 2  # retry after 1x, 2x, then 4x the base interval (120/240/480 s by default)


def context(
    *,
    triggers: list[str],
    sale_window: bool,
    attempts_15m: int,
    verdict_triggers: list[str],
    ai_at: str | None = None,
) -> dict[str, Any]:
    """Stored as Incident.assessment_context after every assessment attempt, successful or not.
    verdict_triggers only moves with a fresh classification: Guard 1 compares against it. ai_at: when
    Gemini was last asked (ISO time), for the retry backoff."""
    return {
        "triggers": sorted(triggers),
        "sale_window": sale_window,
        "attempts_15m": attempts_15m,
        "verdict_triggers": sorted(verdict_triggers),
        "ai_at": ai_at,
    }


def has_real_verdict(inc: Incident) -> bool:
    """A classification Gemini had a hand in: an agreed tie-break (the only AI-dependent verdict that is
    kept through a failed re-check)."""
    return inc.classification is not None and inc.decided_by == DECIDED_BY_AGREE


def ai_due(inc: Incident, now: datetime, base: timedelta) -> bool:
    """Whether Gemini may be asked on this assessment: always, unless its last calls failed and the
    backoff since the last try has not passed. The scorecard does not wait for it."""
    if inc.ai_refresh_failures <= 0:
        return True
    at = (inc.assessment_context or {}).get("ai_at")
    if not at:
        return True
    return now - datetime.fromisoformat(at) >= backoff(base, inc.ai_refresh_failures)


def backoff(base: timedelta, failures: int) -> timedelta:
    return base * 2 ** min(max(failures - 1, 0), MAX_BACKOFF_DOUBLINGS)


def reassess_reason(
    inc: Incident,
    *,
    now: datetime,
    fired: bool,
    sale_active: bool,
    attempts_15m: int,
    new_captures: bool,
    base: timedelta,
    max_age: timedelta = VERDICT_MAX_AGE,
) -> str | None:
    """Why the incident should be re-assessed on this tick, or None to enforce the current verdict only."""
    if inc.last_assessed_at is None:
        return "never_assessed"  # also crash recovery: an incident committed before its first verdict
    since = now - inc.last_assessed_at
    if since < MIN_GAP:
        return None
    ctx = inc.assessment_context or {}
    if set(inc.triggers or []) - set(ctx.get("triggers", [])):
        return "new_trigger"
    if "sale_window" in ctx and ctx["sale_window"] != sale_active:
        return "sale_window_changed"
    if inc.ai_refresh_failures > 0 and fired and since >= backoff(base, inc.ai_refresh_failures):
        # The last Gemini call failed: retry it on a backoff (the breaker may still be open). Material
        # changes below still re-run the scorecard meanwhile; ai_due keeps Gemini out of those.
        return "retry_after_ai_failure"
    if since < base:
        return None
    confidence = inc.confidence or 0.0
    if inc.classification in HARMLESS_CLASSES and confidence >= CONFIDENCE_ACT:
        # A confident harmless verdict (policy R2: no friction) and a real sale keeps growing and capturing:
        # re-asking on that would cost a call every interval for nothing. An attack starting shows up as a
        # new trigger. Below the floor R2 falls through to R3 (3-D Secure), so it is re-asked like "unclear".
        return "verdict_aged" if fired and _aged(inc, now, max_age) else None
    if confidence < CONFIDENCE_ACT and attempts_15m >= GROWTH * max(ctx.get("attempts_15m", 0), 1):
        return "attempts_grew"
    if confidence < CONFIDENCE_REFUND and new_captures:
        return "new_captures"  # money is getting through and the verdict isn't strong enough to refund it
    if fired and _aged(inc, now, max_age):
        return "verdict_aged"
    return None


def _aged(inc: Incident, now: datetime, max_age: timedelta) -> bool:
    return inc.verdict_at is None or now - inc.verdict_at >= max_age


def contradicting_triggers(inc: Incident) -> set[str]:
    """Guard 1: triggers that fired after the verdict was made and point the other way. A kept harmless
    verdict must not survive the start of an attack, and a kept attack verdict must not survive PayPal
    starting to fail (it would block and refund real shoppers retrying through the outage)."""
    if inc.classification in HARMLESS_CLASSES:
        against = FRAUD_TRIGGERS
    elif inc.classification in ATTACK_CLASSES:
        against = OUTAGE_TRIGGERS
    else:
        return set()
    seen = set((inc.assessment_context or {}).get("verdict_triggers", []))
    return (set(inc.triggers or []) & against) - seen


def unjudged_triggers(inc: Incident, fired: list[str]) -> list[str]:
    """Triggers policy R0 may act on before the scorecard re-assesses: all of them, minus those a confident
    harmless verdict has already weighed (otherwise every re-check of a real rush would switch 3-D Secure
    on, then RELAX would switch it off again)."""
    if inc.classification in HARMLESS_CLASSES and (inc.confidence or 0) >= CONFIDENCE_ACT:
        seen = set((inc.assessment_context or {}).get("verdict_triggers", []))
        return [t for t in fired if t not in seen]
    return list(fired)


def verdict_stale(inc: Incident, now: datetime) -> bool:
    """Guard 2: a kept verdict the AI has failed to re-confirm 3 times in a row, or for 20 minutes."""
    if inc.ai_refresh_failed_at is None:
        return False
    if inc.ai_refresh_failures >= STALE_AFTER_FAILURES:
        return True
    return inc.verdict_at is not None and now - inc.verdict_at >= STALE_AFTER
