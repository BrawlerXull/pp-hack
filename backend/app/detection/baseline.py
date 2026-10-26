"""Loads recent attempts and the shop's "normal" baseline from its own history.

The baseline must describe the shop's customers, not its attackers: otherwise every attack makes the next
one look normal (a past card-testing run's ~70% decline rate lifts the decline_spike limit above what the
next run reaches). So history excludes, using only what the guard itself recorded (never simulator labels,
which a real shop doesn't have and would leak the answer into the test):
  - attempts inside any incident window [opened - 15 min, closed or now]
  - attempts the guard stopped (BLOCKED, CHALLENGED) and devices that are blocked right now
  - the last 2 hours, so an attack the detector has not opened an incident for yet can't become "normal"
and only CAPTURED attempts make a device or email domain "known" (a refunded capture was the attacker's).
Hours an incident window covers are left out of the hourly series (not counted as zero-traffic hours,
which would lower the mean and inflate velocity_z). The known-customer sets drop only the windows of
incidents not confidently judged harmless: a shopper whose first purchase fell in a flash sale the AI
called genuine_rush is a returning customer next time, not a stranger R4 may block and refund.
"""

import math
from datetime import datetime, timedelta

from sqlalchemy import and_, exists, func, literal, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CheckoutAttempt, Incident, Outcome
from app.detection.features import AttemptRow, Baseline, card_key
from app.guard import devices
from app.policy.engine import CONFIDENCE_ACT, HARMLESS_CLASSES

HISTORY_DAYS = 28
# History closer than this is excluded so an ongoing attack can't become "normal".
EXCLUDE_RECENT = timedelta(hours=2)
# Same lookback the signature and refunds use: an attack's first attempts land before the incident opens.
INCIDENT_LEAD = timedelta(minutes=15)


async def recent_rows(session: AsyncSession, shop_id: str, since: datetime) -> list[AttemptRow]:
    q = select(
        CheckoutAttempt.ts,
        CheckoutAttempt.sku,
        CheckoutAttempt.amount,
        CheckoutAttempt.method,
        CheckoutAttempt.fingerprint_hash,
        CheckoutAttempt.email_domain,
        CheckoutAttempt.card_bin6,
        CheckoutAttempt.card_last4,
        CheckoutAttempt.card_expiry,
        CheckoutAttempt.payer_id_hash,
        CheckoutAttempt.outcome,
        CheckoutAttempt.decline_code,
        CheckoutAttempt.decline_category,
    ).where(CheckoutAttempt.shop_id == shop_id, CheckoutAttempt.ts > since)
    return [
        AttemptRow(
            ts=r.ts,
            sku=r.sku,
            amount=float(r.amount),
            method=r.method,
            fingerprint_hash=r.fingerprint_hash,
            email_domain=r.email_domain,
            card_key=card_key(r.card_bin6, r.card_last4, r.card_expiry),
            payer_id_hash=r.payer_id_hash,
            outcome=r.outcome,
            decline_code=r.decline_code,
            decline_category=r.decline_category,
        )
        for r in (await session.execute(q)).all()
    ]


async def load_baseline(session: AsyncSession, shop_id: str, now: datetime) -> Baseline:
    end = now - EXCLUDE_RECENT
    start = end - timedelta(days=HISTORY_DAYS)
    blocked = list(await devices.blocked(session, shop_id))
    window = (
        Incident.shop_id == shop_id,
        CheckoutAttempt.ts >= Incident.opened_at - INCIDENT_LEAD,
        CheckoutAttempt.ts <= func.coalesce(Incident.closed_at, literal(now)),
    )
    in_incident = exists().where(*window)
    judged_harmless = and_(
        Incident.classification.in_(HARMLESS_CLASSES), func.coalesce(Incident.confidence, 0) >= CONFIDENCE_ACT
    )
    in_suspect_incident = exists().where(
        *window, or_(Incident.classification.is_(None), not_(judged_harmless))
    )
    eligible = (
        CheckoutAttempt.shop_id == shop_id,
        CheckoutAttempt.ts > start,
        CheckoutAttempt.ts <= end,
        CheckoutAttempt.outcome.not_in([Outcome.BLOCKED, Outcome.CHALLENGED]),
        *([CheckoutAttempt.fingerprint_hash.not_in(blocked)] if blocked else []),
    )
    in_range = (*eligible, ~in_incident)  # rate and volume statistics: no incident window at all

    hour = func.date_trunc("hour", CheckoutAttempt.ts)
    hourly = (await session.execute(select(hour, func.count()).where(*in_range).group_by(hour))).all()
    if not hourly:
        return Baseline()

    first = min(h for h, _ in hourly)
    span_hours = max(1, int((end - first).total_seconds() // 3600))
    windows = await _incident_windows(session, shop_id, first, now)
    counts = {h: c for h, c in hourly}
    hours = [first + timedelta(hours=i) for i in range(span_hours)]
    series = [counts.get(h, 0) for h in hours if not _covered(h, windows)] or [c for _, c in hourly]
    mean = sum(series) / len(series)
    std = math.sqrt(sum((c - mean) ** 2 for c in series) / len(series))

    totals = (
        await session.execute(
            select(
                func.count().filter(CheckoutAttempt.outcome == Outcome.DECLINED),
                func.count().filter(CheckoutAttempt.outcome == Outcome.ERROR),
                func.count(),
            ).where(*in_range)
        )
    ).one()
    declined, errors, reached = totals
    good = (*eligible, ~in_suspect_incident, CheckoutAttempt.outcome == Outcome.CAPTURED)
    domains = (
        await session.execute(
            select(CheckoutAttempt.email_domain)
            .where(*good, CheckoutAttempt.email_domain.is_not(None))
            .distinct()
        )
    ).scalars()
    fps = (await session.execute(select(CheckoutAttempt.fingerprint_hash).where(*good).distinct())).scalars()

    return Baseline(
        attempts_per_hour_mean=max(mean, 0.5),
        attempts_per_hour_std=max(std, 1.0),
        decline_rate=declined / reached if reached else 0.05,
        error_rate=errors / reached if reached else 0.01,
        known_domains=frozenset(domains),
        known_fingerprints=frozenset(fps),
        history_days=max(1, span_hours // 24),
    )


async def _incident_windows(
    session: AsyncSession, shop_id: str, since: datetime, now: datetime
) -> list[tuple[datetime, datetime]]:
    q = select(Incident.opened_at, Incident.closed_at).where(
        Incident.shop_id == shop_id, func.coalesce(Incident.closed_at, literal(now)) >= since
    )
    return [(opened - INCIDENT_LEAD, closed or now) for opened, closed in (await session.execute(q)).all()]


def _covered(hour: datetime, windows: list[tuple[datetime, datetime]]) -> bool:
    """By overlap, not by bucket key: date_trunc buckets follow the DB session's time zone."""
    return any(hour < end and hour + timedelta(hours=1) > start for start, end in windows)
