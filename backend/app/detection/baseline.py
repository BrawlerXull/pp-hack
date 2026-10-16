"""Loads recent attempts and the shop's "normal" baseline from its own history."""

import math
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CheckoutAttempt, Outcome
from app.detection.features import AttemptRow, Baseline

HISTORY_DAYS = 28
# History closer than this is excluded so an ongoing attack can't become "normal".
EXCLUDE_RECENT = timedelta(hours=2)


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
        CheckoutAttempt.payer_id_hash,
        CheckoutAttempt.outcome,
        CheckoutAttempt.decline_code,
    ).where(CheckoutAttempt.shop_id == shop_id, CheckoutAttempt.ts > since)
    return [
        AttemptRow(
            ts=r.ts,
            sku=r.sku,
            amount=float(r.amount),
            method=r.method,
            fingerprint_hash=r.fingerprint_hash,
            email_domain=r.email_domain,
            card_key=f"{r.card_bin6}{r.card_last4}" if r.card_bin6 else None,
            payer_id_hash=r.payer_id_hash,
            outcome=r.outcome,
            decline_code=r.decline_code,
        )
        for r in (await session.execute(q)).all()
    ]


async def load_baseline(session: AsyncSession, shop_id: str, now: datetime) -> Baseline:
    end = now - EXCLUDE_RECENT
    start = end - timedelta(days=HISTORY_DAYS)
    in_range = (CheckoutAttempt.shop_id == shop_id, CheckoutAttempt.ts > start, CheckoutAttempt.ts <= end)

    hour = func.date_trunc("hour", CheckoutAttempt.ts)
    hourly = (await session.execute(select(hour, func.count()).where(*in_range).group_by(hour))).all()
    if not hourly:
        return Baseline()

    first = min(h for h, _ in hourly)
    span_hours = max(1, int((end - first).total_seconds() // 3600))
    counts = {h: c for h, c in hourly}
    series = [counts.get(first + timedelta(hours=i), 0) for i in range(span_hours)]
    mean = sum(series) / len(series)
    std = math.sqrt(sum((c - mean) ** 2 for c in series) / len(series))

    totals = (
        await session.execute(
            select(
                func.count().filter(CheckoutAttempt.outcome == Outcome.DECLINED),
                func.count().filter(CheckoutAttempt.outcome == Outcome.ERROR),
                func.count().filter(CheckoutAttempt.outcome.not_in([Outcome.BLOCKED, Outcome.CHALLENGED])),
            ).where(*in_range)
        )
    ).one()
    declined, errors, reached = totals
    good = (*in_range, CheckoutAttempt.outcome.in_([Outcome.CAPTURED, Outcome.REFUNDED]))
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
