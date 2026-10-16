"""Seed synthetic "normal" history so the baseline has something to compare against.

Rows are tagged source=seed and paypal_live=false: they are labelled simulated history, never shown
as real PayPal transactions.
"""

import random
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CheckoutAttempt, Decision, Outcome, Source
from app.shop.catalog import PRODUCTS
from app.shop.service import email_domain, fingerprint
from app.simulator.scenarios import BROWSER_UA, customer_pool, luhn_card

# Orders per hour of day for a small candle shop (evening-heavy, quiet overnight).
HOURLY_WEIGHTS = [0.1, 0.05, 0.05, 0.05, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.5, 0.6,
                  0.6, 0.5, 0.5, 0.5, 0.6, 0.7, 0.9, 1.0, 1.0, 0.8, 0.5, 0.2]  # fmt: skip


async def seed_history(session: AsyncSession, shop_id: str, days: int = 28, orders_per_day: int = 8) -> int:
    await session.execute(
        delete(CheckoutAttempt).where(
            CheckoutAttempt.shop_id == shop_id, CheckoutAttempt.source == Source.SEED
        )
    )
    rng = random.Random(42)
    pool = customer_pool()
    skus = list(PRODUCTS)
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    total_weight = sum(HOURLY_WEIGHTS)
    rows = 0
    for day in range(days, 0, -1):
        for hour, w in enumerate(HOURLY_WEIGHTS):
            expected = orders_per_day * w / total_weight
            n = sum(rng.random() < expected / 3 for _ in range(3))
            for _ in range(n):
                device, email = rng.choice(pool)
                ts = (now - timedelta(days=day)).replace(hour=hour) + timedelta(minutes=rng.randint(0, 59))
                declined = rng.random() < 0.03
                product = PRODUCTS[rng.choices(skus, weights=[2, 5, 4])[0]]
                card = luhn_card("4", rng=rng)
                session.add(
                    CheckoutAttempt(
                        shop_id=shop_id,
                        ts=ts,
                        source=Source.SEED,
                        paypal_live=False,
                        sku=product.sku,
                        amount=product.price,
                        method="card" if rng.random() < 0.6 else "wallet",
                        fingerprint_hash=fingerprint(device, BROWSER_UA),
                        email_domain=email_domain(email),
                        card_bin6=card[:6],
                        card_last4=card[-4:],
                        guard_decision=Decision.ALLOW,
                        guard_reason="normal",
                        outcome=Outcome.DECLINED if declined else Outcome.CAPTURED,
                        decline_code="INSUFFICIENT_FUNDS" if declined else None,
                    )
                )
                rows += 1
    await session.commit()
    return rows
