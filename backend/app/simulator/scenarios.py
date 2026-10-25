"""Attack and control scenarios. SIMULATION: every attempt is tagged source=simulator.

mode="live"    → real PayPal sandbox calls with sandbox test cards (declines via CCREJECT-* name triggers)
mode="offline" → no PayPal call; outcome is synthesised and the row is stored with paypal_live=false

Declines use the same CCREJECT-* trigger in both modes; offline derives the code the sandbox would
return from it through app/paypal/codes.py, so both modes store the same canonical decline names.
"""

import random
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Literal

from app.paypal import codes
from app.shop.catalog import CHEAPEST_SKU, PRODUCTS

Mode = Literal["live", "offline"]
# PayPal's published sandbox Visa test numbers (developer.paypal.com/tools/sandbox/card-testing/).
# Live mode rotates through them with a varied expiry, so a live attack shows "a new card on almost
# every attempt" like a real one (card_key includes expiry, see detection/features.py). Whether the
# sandbox accepts every number is still to confirm once card processing is enabled.
SANDBOX_TEST_CARDS = (
    "4005519200000004",
    "4012000033330026",
    "4012000077777777",
    "4012888888881881",
    "4217651111111119",
    "4500600000000061",
    "4772129056533503",
    "4915805038587737",
)
DECLINE_TRIGGERS = [
    "CCREJECT-REFUSED",
    "CCREJECT-SF",
    "CCREJECT-IF",
    "CCREJECT-LS",
    "CCREJECT-CVV_F",
    "CCREJECT-EC",
]
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
    card_expiry: str = "2030-12"
    payer_id: str | None = None
    # offline plan (also used as the intended outcome in live mode). "pending": a wallet order the buyer
    # never approves (stays CREATED), e.g. a stolen login that fails PayPal's own checks.
    outcome: Literal["approve", "decline", "error", "pending"] = "approve"
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


def _expiry(rng: random.Random) -> str:
    return f"{rng.randint(2028, 2033)}-{rng.randint(1, 12):02d}"


def live_card(rng: random.Random) -> tuple[str, str]:
    """(number, expiry) for a card tester: any published test number, any future expiry."""
    return rng.choice(SANDBOX_TEST_CARDS), _expiry(rng)


def customer_card(device_id: str) -> tuple[str, str]:
    """(number, expiry) for a returning shopper in live mode: the same card on every visit."""
    rng = random.Random(device_id)
    return rng.choice(SANDBOX_TEST_CARDS), _expiry(rng)


def trigger_decline(card_name: str) -> str:
    """Canonical decline name the sandbox gives a CCREJECT-* card name (same table as live results)."""
    code = codes.lookup(codes.trigger_code(card_name))
    return code.name if code else "INSTRUMENT_DECLINED"


def customer_pool(n: int = 60, seed: int = 7) -> list[tuple[str, str]]:
    """Stable set of returning customers: (device_id, email). Shared by seed history and flash sale."""
    rng = random.Random(seed)
    names = ["ava", "liam", "mia", "noah", "zoe", "eli", "ivy", "leo", "ana", "sam", "kai", "nia"]
    return [
        (f"cust-{i:03d}", f"{rng.choice(names)}{rng.randint(1, 999)}@{rng.choice(COMMON_DOMAINS)}")
        for i in range(n)
    ]


def human_gap(rng: random.Random, mean_s: float) -> float:
    """Seconds until the next real shopper: independent people arrive as a Poisson process (exponential
    gaps, coefficient of variation about 1), not on a timer. Floored so a live run never fires two orders
    in the same instant."""
    return max(0.2, rng.expovariate(1 / mean_s))


def honest_decline(rng: random.Random) -> tuple[str, str]:
    """(card name trigger, wallet 422 issue) for a real shopper's refusal. Most honest refusals are a bank's
    generic "do not honor" with no reason given, the rest insufficient funds: detection must not read the
    generic ones as card testing."""
    if rng.random() < 0.6:
        return "CCREJECT-REFUSED", "INSTRUMENT_DECLINED"
    return "CCREJECT-IF", "INSUFFICIENT_FUNDS"


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
        number, expiry = (luhn_card("4", rng=rng), "2030-12") if mode == "offline" else live_card(rng)
        name = "Test Buyer" if approve else DECLINE_TRIGGERS[i % len(DECLINE_TRIGGERS)]
        yield SimAttempt(
            delay_s=rng.uniform(0.3, 1.2) / speed,
            sku=CHEAPEST_SKU,
            method="card",
            device_id=farm[i % len(farm)],
            user_agent=BOT_UA,
            email=_bot_email(rng),
            card_number=number,
            card_name=name,
            card_expiry=expiry,
            outcome="approve" if approve else "decline",
            decline_code=trigger_decline(name),
            is_bot=True,
        )


def account_testing(
    mode: Mode, count: int = 80, seed: int | None = None, speed: float = 1.0
) -> Iterator[SimAttempt]:
    """Stolen PayPal accounts (wallet). Offline only: wallet approval needs a real buyer login.
    Most stolen logins never get through PayPal's own login checks, so most orders stay CREATED
    (unapproved); a few are refused and only a few are captured."""
    rng = random.Random(seed)
    farm = [f"bot-{rng.randint(1000, 9999)}" for _ in range(3)]
    for i in range(count):
        roll = rng.random()
        outcome: Literal["approve", "decline", "pending"] = (
            "approve" if roll < 0.12 else "decline" if roll < 0.30 else "pending"
        )
        yield SimAttempt(
            delay_s=rng.uniform(0.4, 1.4) / speed,
            sku=CHEAPEST_SKU,
            method="wallet",
            device_id=farm[i % len(farm)],
            user_agent=BOT_UA,
            email=_bot_email(rng),
            payer_id=f"PAYER{rng.randint(10**9, 10**10)}",
            outcome=outcome,
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
    """Real customers after an announced sale: returning shoppers, varied baskets, a few honest declines
    (mostly the bank's generic refusal)."""
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
        approve = rng.random() > 0.07
        refusal, issue = honest_decline(rng)
        number, expiry = (luhn_card("4", rng=rng), "2030-12") if mode == "offline" else customer_card(device)
        yield SimAttempt(
            delay_s=human_gap(rng, 1.65) / speed,
            sku=rng.choices(skus, weights=[2, 5, 4])[0],
            method=method,
            device_id=device,
            user_agent=BROWSER_UA,
            email=email,
            card_number=number if method == "card" else None,
            card_expiry=expiry,
            card_name="Test Buyer" if approve else refusal,
            payer_id=f"PAYER{rng.randint(10**9, 10**10)}" if method == "wallet" else None,
            outcome="approve" if approve else "decline",
            decline_code=issue,
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
            delay_s=human_gap(rng, 2.0) / speed,
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
        number, expiry = (luhn_card("4", rng=rng), "2030-12") if mode == "offline" else customer_card(device)
        yield SimAttempt(
            delay_s=human_gap(rng, 12.5) / speed,
            sku=rng.choice(list(PRODUCTS)),
            method="card",
            device_id=device,
            user_agent=BROWSER_UA,
            email=email,
            card_number=number,
            card_expiry=expiry,
            card_name="Test Buyer",
        )


def mixed_traffic(
    mode: Mode, count: int = 40, seed: int | None = None, speed: float = 1.0
) -> Iterator[SimAttempt]:
    """A busy but ordinary evening: returning and new shoppers, varied baskets, and a few honest retries
    (a card refused, usually with the bank's generic code, then a second card from the same device that
    goes through)."""
    rng = random.Random(seed)
    pool = customer_pool()
    skus = list(PRODUCTS)
    for _ in range(count):
        device, email = (
            rng.choice(pool)
            if rng.random() < 0.6
            else (
                f"new-{rng.randint(1000, 9999)}",
                f"shopper{rng.randint(1, 9999)}@{rng.choice(COMMON_DOMAINS)}",
            )
        )
        sku = rng.choices(skus, weights=[2, 5, 4])[0]
        delay = human_gap(rng, 8) / speed
        retry = rng.random() < 0.12
        tries = ["decline", "approve"] if retry else ["approve"]
        refusal, issue = honest_decline(rng)
        for n, outcome in enumerate(tries):
            number, expiry = (
                (luhn_card("4", rng=rng), "2030-12") if mode == "offline" else customer_card(f"{device}{n}")
            )
            yield SimAttempt(
                delay_s=delay if n == 0 else rng.uniform(20, 60) / speed,
                sku=sku,
                method="card",
                device_id=device,
                user_agent=BROWSER_UA,
                email=email,
                card_number=number,
                card_expiry=expiry,
                card_name="Test Buyer" if outcome == "approve" else refusal,
                outcome="approve" if outcome == "approve" else "decline",
                decline_code=issue,
            )


def stealth_bot(
    mode: Mode, count: int = 25, seed: int | None = None, speed: float = 1.0
) -> Iterator[SimAttempt]:
    """A careful card tester: a fresh device id and an ordinary browser user agent on every attempt (so
    per-device counts see one card each), one attempt every one to five minutes, cheapest item."""
    rng = random.Random(seed)
    for i in range(count):
        approve = rng.random() < 0.28
        number, expiry = (luhn_card("4", rng=rng), "2030-12") if mode == "offline" else live_card(rng)
        name = "Test Buyer" if approve else DECLINE_TRIGGERS[i % len(DECLINE_TRIGGERS)]
        yield SimAttempt(
            delay_s=rng.uniform(60, 300) / speed,
            sku=CHEAPEST_SKU,
            method="card",
            device_id=f"anon-{rng.randint(10**6, 10**7)}",
            user_agent=BROWSER_UA,
            email=_bot_email(rng),
            card_number=number,
            card_name=name,
            card_expiry=expiry,
            outcome="approve" if approve else "decline",
            decline_code=trigger_decline(name),
            is_bot=True,
        )


SCENARIOS = {
    "card_testing": card_testing,
    "account_testing": account_testing,
    "slow_drip": slow_drip,
    "flash_sale": flash_sale,
    "paypal_outage": paypal_outage,
    "normal_night": normal_night,
    "mixed_traffic": mixed_traffic,
    "stealth_bot": stealth_bot,
}
# Scenarios that can't run against the live sandbox (they'd need real buyer logins or a real outage).
OFFLINE_ONLY = {"account_testing", "paypal_outage"}
