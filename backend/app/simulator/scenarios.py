"""Attack and control scenarios. SIMULATION: every attempt is tagged source=simulator.

mode="live"    → real PayPal sandbox calls with sandbox test cards (declines via CCREJECT-* name triggers)
mode="offline" → no PayPal call; outcome is synthesised and the row is stored with paypal_live=false
"""

import random
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Literal

from app.shop.catalog import CHEAPEST_SKU, PRODUCTS

Mode = Literal["live", "offline"]
SANDBOX_APPROVE_CARD = "4012888888881881"
DECLINE_TRIGGERS = [
    "CCREJECT-REFUSED",
    "CCREJECT-SF",
    "CCREJECT-IF",
    "CCREJECT-LS",
    "CCREJECT-CVV_F",
    "CCREJECT-EC",
]
OFFLINE_DECLINE_CODES = ["INSTRUMENT_DECLINED", "CARD_DECLINED", "TRANSACTION_REFUSED", "CARD_EXPIRED"]
COMMON_DOMAINS = ["gmail.com", "outlook.com", "yahoo.com", "icloud.com", "hotmail.com", "proton.me"]
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 Safari/605.1.15"
BOT_UA = "Mozilla/5.0 (X11; Linux x86_64) HeadlessChrome/131.0 Safari/537.36"


@dataclass
class SimAttempt:
    delay_s: float
    sku: str
    method: Literal["card", "wallet"]
    device_id: str
    user_agent: str
    email: str
    card_number: str | None = None
    card_name: str | None = None
    payer_id: str | None = None
    # offline plan (also used as the intended outcome in live mode)
    outcome: Literal["approve", "decline", "error"] = "approve"
    decline_code: str = "INSTRUMENT_DECLINED"
    is_bot: bool = False
    extra: dict[str, str] = field(default_factory=dict)


def luhn_card(prefix: str = "4", length: int = 16, rng: random.Random | None = None) -> str:
    rng = rng or random.Random()
    digits = [int(c) for c in prefix] + [rng.randint(0, 9) for _ in range(length - len(prefix) - 1)]
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 0:
            d *= 2
            d -= 9 if d > 9 else 0
        total += d
    return "".join(map(str, digits)) + str((10 - total % 10) % 10)


def customer_pool(n: int = 60, seed: int = 7) -> list[tuple[str, str]]:
    """Stable set of returning customers: (device_id, email). Shared by seed history and flash sale."""
    rng = random.Random(seed)
    names = ["ava", "liam", "mia", "noah", "zoe", "eli", "ivy", "leo", "ana", "sam", "kai", "nia"]
    return [
        (f"cust-{i:03d}", f"{rng.choice(names)}{rng.randint(1, 999)}@{rng.choice(COMMON_DOMAINS)}")
        for i in range(n)
    ]


def _bot_email(rng: random.Random) -> str:
    junk = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(6))
    tld = rng.choice(["top", "xyz", "click", "shop", "info", "site"])
    return f"{junk}@{junk[:3]}mail-{rng.randint(10, 99)}.{tld}"


def card_testing(
    mode: Mode, count: int = 120, seed: int | None = None, speed: float = 1.0
) -> Iterator[SimAttempt]:
    """Bot farm (a few headless devices) hammering the cheapest SKU with a fresh card each time."""
    rng = random.Random(seed)
    farm = [f"bot-{rng.randint(1000, 9999)}" for _ in range(4)]
    for i in range(count):
        approve = rng.random() < 0.28
        number = luhn_card("4", rng=rng) if mode == "offline" else SANDBOX_APPROVE_CARD
        yield SimAttempt(
            delay_s=rng.uniform(0.3, 1.2) / speed,
            sku=CHEAPEST_SKU,
            method="card",
            device_id=farm[i % len(farm)],
            user_agent=BOT_UA,
            email=_bot_email(rng),
            card_number=number,
            card_name="Test Buyer" if approve else DECLINE_TRIGGERS[i % len(DECLINE_TRIGGERS)],
            outcome="approve" if approve else "decline",
            decline_code=rng.choice(OFFLINE_DECLINE_CODES),
            is_bot=True,
        )


def account_testing(
    mode: Mode, count: int = 80, seed: int | None = None, speed: float = 1.0
) -> Iterator[SimAttempt]:
    """Stolen PayPal accounts (wallet). Offline only: wallet approval needs a real buyer login."""
    rng = random.Random(seed)
    farm = [f"bot-{rng.randint(1000, 9999)}" for _ in range(3)]
    for i in range(count):
        approve = rng.random() < 0.35
        yield SimAttempt(
            delay_s=rng.uniform(0.4, 1.4) / speed,
            sku=CHEAPEST_SKU,
            method="wallet",
            device_id=farm[i % len(farm)],
            user_agent=BOT_UA,
            email=_bot_email(rng),
            payer_id=f"PAYER{rng.randint(10**9, 10**10)}",
            outcome="approve" if approve else "decline",
            decline_code=rng.choice(["PAYER_CANNOT_PAY", "INSTRUMENT_DECLINED", "PAYER_ACCOUNT_RESTRICTED"]),
            is_bot=True,
        )


def slow_drip(
    mode: Mode, count: int = 40, seed: int | None = None, speed: float = 1.0
) -> Iterator[SimAttempt]:
    for a in card_testing(mode, count, seed, speed):
        a.delay_s = 12 / speed
        yield a


def flash_sale(
    mode: Mode, count: int = 60, seed: int | None = None, speed: float = 1.0
) -> Iterator[SimAttempt]:
    """Real customers after an announced sale: returning shoppers, varied baskets, few declines."""
    rng = random.Random(seed)
    pool = customer_pool()
    skus = list(PRODUCTS)
    for _ in range(count):
        device, email = (
            rng.choice(pool)
            if rng.random() < 0.7
            else (
                f"new-{rng.randint(1000, 9999)}",
                f"shopper{rng.randint(1, 9999)}@{rng.choice(COMMON_DOMAINS)}",
            )
        )
        method = rng.choice(["card", "wallet"]) if mode == "offline" else "card"
        approve = rng.random() > 0.04
        yield SimAttempt(
            delay_s=rng.uniform(0.8, 2.5) / speed,
            sku=rng.choices(skus, weights=[2, 5, 4])[0],
            method=method,
            device_id=device,
            user_agent=BROWSER_UA,
            email=email,
            card_number=(luhn_card("4", rng=rng) if mode == "offline" else SANDBOX_APPROVE_CARD)
            if method == "card"
            else None,
            card_name="Test Buyer" if approve else "CCREJECT-IF",
            payer_id=f"PAYER{rng.randint(10**9, 10**10)}" if method == "wallet" else None,
            outcome="approve" if approve else "decline",
            decline_code="INSUFFICIENT_FUNDS",
        )


def paypal_outage(
    mode: Mode, count: int = 30, seed: int | None = None, speed: float = 1.0
) -> Iterator[SimAttempt]:
    """Normal customers during a (simulated) PayPal processing outage: errors, not declines."""
    rng = random.Random(seed)
    pool = customer_pool()
    for _ in range(count):
        device, email = rng.choice(pool)
        yield SimAttempt(
            delay_s=rng.uniform(1.0, 3.0) / speed,
            sku=rng.choice(list(PRODUCTS)),
            method="card",
            device_id=device,
            user_agent=BROWSER_UA,
            email=email,
            card_number=luhn_card("4", rng=rng),
            card_name="Test Buyer",
            outcome="error" if rng.random() < 0.7 else "approve",
        )


def normal_night(
    mode: Mode, count: int = 4, seed: int | None = None, speed: float = 1.0
) -> Iterator[SimAttempt]:
    rng = random.Random(seed)
    pool = customer_pool()
    for _ in range(count):
        device, email = rng.choice(pool)
        yield SimAttempt(
            delay_s=rng.uniform(5, 20) / speed,
            sku=rng.choice(list(PRODUCTS)),
            method="card",
            device_id=device,
            user_agent=BROWSER_UA,
            email=email,
            card_number=luhn_card("4", rng=rng) if mode == "offline" else SANDBOX_APPROVE_CARD,
            card_name="Test Buyer",
        )


SCENARIOS = {
    "card_testing": card_testing,
    "account_testing": account_testing,
    "slow_drip": slow_drip,
    "flash_sale": flash_sale,
    "paypal_outage": paypal_outage,
    "normal_night": normal_night,
}
# Scenarios that can't run against the live sandbox (they'd need real buyer logins or a real outage).
OFFLINE_ONLY = {"account_testing", "paypal_outage"}
