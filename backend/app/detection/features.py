"""Deterministic feature extraction over rolling windows of checkout attempts.

Everything here is computed from our own attempt rows. IP addresses are deliberately not a feature: the
shop's ip_hash comes from the left-most X-Forwarded-For entry (app/api/shop.client_ip), which the client
sets freely, so "cards per IP" would be whatever the attacker wants it to be. Devices, cards and PayPal
payers are counted separately and never mixed.
"""

import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from app.db.models import DeclineCategory, Outcome
from app.detection import stats
from app.paypal.codes import CLIENT_STATE_ERRORS
from app.shop.catalog import CHEAPEST_SKU, PRODUCTS

# 10 min is the fraud-decline window (triggers.fraud_declines); the others are the original rolling views.
WINDOWS_MIN = (1, 5, 10, 15, 60)


class FeatureKey(StrEnum):
    """Every number the AI may cite as evidence. The analyst's evidence is validated against these."""

    ATTEMPTS = "attempts"
    ATTEMPTS_PER_MIN = "attempts_per_min"
    EXPECTED_ATTEMPTS = "expected_attempts"
    VELOCITY_SURPRISE = "velocity_surprise"
    DECLINE_RATE = "decline_rate"
    DECLINE_SURPRISE = "decline_surprise"
    FRAUD_DECLINES = "fraud_declines"
    FRAUD_DECLINE_SHARE = "fraud_decline_share"
    ERROR_RATE = "error_rate"
    CAPTURED = "captured"
    BLOCKED_OR_CHALLENGED = "blocked_or_challenged"
    SCA_REQUIRED = "sca_required"
    UNIQUE_FINGERPRINTS = "unique_fingerprints"
    TOP_FINGERPRINT_SHARE = "top_fingerprint_share"
    UNIQUE_CARDS_RATIO = "unique_cards_ratio"
    UNIQUE_PAYERS_RATIO = "unique_payers_ratio"
    TOP_SKU_SHARE = "top_sku_share"
    CHEAPEST_SKU_SHARE = "cheapest_sku_share"
    CHEAPEST_SKU_ATTEMPTS = "cheapest_sku_attempts"
    CHEAPEST_SKU_CARDS = "cheapest_sku_cards"
    CHEAPEST_SKU_PAYERS = "cheapest_sku_payers"
    CHEAPEST_SKU_DECLINE_RATE = "cheapest_sku_decline_rate"
    AVG_AMOUNT = "avg_amount"
    NEW_EMAIL_DOMAIN_SHARE = "new_email_domain_share"
    KNOWN_CUSTOMER_SHARE = "known_customer_share"
    WALLET_SHARE = "wallet_share"
    # P9: per-device credentials, wallet approvals, timing, error spread, baskets, decline mix.
    CARDS_PER_DEVICE_MAX = "cards_per_device_max"
    CARDS_PER_DEVICE_P90 = "cards_per_device_p90"
    MULTI_CARD_DEVICES = "multi_card_devices"
    PAYERS_PER_DEVICE_MAX = "payers_per_device_max"
    PAYERS_PER_DEVICE_P90 = "payers_per_device_p90"
    AGED_WALLET_ORDERS = "aged_wallet_orders"
    UNAPPROVED_WALLET_SHARE = "unapproved_wallet_share"
    INTERARRIVAL_CV = "interarrival_cv"
    ERROR_DEVICES = "error_devices"
    ERROR_TOP_DEVICE_SHARE = "error_top_device_share"
    ERROR_KNOWN_SHARE = "error_known_share"
    ERROR_TOP_SKU_SHARE = "error_top_sku_share"
    DISTINCT_SKUS = "distinct_skus"
    SKU_ENTROPY = "sku_entropy"
    AMOUNT_CV = "amount_cv"
    PROBING_DECLINE_SHARE = "probing_decline_share"
    CUSTOMER_DECLINE_SHARE = "customer_decline_share"
    PROCESSOR_SHARE = "processor_share"
    NON_CUSTOMER_DECLINES = "non_customer_declines"
    SUSPECT_DECLINES = "suspect_declines"
    SUSPECT_DECLINE_DEVICES = "suspect_decline_devices"
    CLIENT_ERRORS = "client_errors"
    SLOW_DECLINE_CUSUM = "slow_decline_cusum"


# Outcomes that mean "stopped by 3-D Secure" (the sca_required feature).
SCA_STOPPED = frozenset({Outcome.SCA_REQUIRED, Outcome.SCA_FAILED})


def card_key(bin6: str | None, last4: str | None, expiry: str | None, brand: str | None = None) -> str | None:
    """Identity of a card for unique-card counts. Expiry is part of it on purpose: card testers try
    many expiry dates against one number, and each guess is a separate credential to the issuer.
    A shopper whose card is reissued counts as a new card once, which is harmless.
    A browser card (Card Fields) is known only by what PayPal returns: when PayPal sends no BIN, the brand
    stands in for it. Two cards of one brand sharing the last 4 then count as one, which only ever
    under-counts cards (never invents card cycling)."""
    prefix = bin6 or (f"{brand}*" if brand else None)
    if not prefix or not last4:
        return None
    return f"{prefix}{last4}:{expiry}" if expiry else f"{prefix}{last4}"


def card_number(key: str) -> str:
    """The card behind a card_key, without the expiry. Per-device counts use this: a shopper who mistypes
    the expiry twice is still one card, while a bot cycling numbers is many."""
    return key.split(":", 1)[0]


@dataclass(frozen=True)
class AttemptRow:
    ts: datetime
    sku: str
    amount: float
    method: str
    fingerprint_hash: str
    email_domain: str | None
    card_key: str | None  # card_key(): bin6 (or brand)+last4+expiry, never the full number
    payer_id_hash: str | None
    outcome: str
    decline_code: str | None
    decline_category: str | None = None  # DeclineCategory from app/paypal/codes.py, on DECLINED rows


# A tiny shop's own rate (8 orders a day) would make two orders in five minutes "impossible": the velocity
# test assumes at least this many attempts an hour are normal.
MIN_EXPECTED_PER_HOUR = 6.0
# The decline test assumes at least this decline rate is normal: honest card declines (mostly the bank's
# generic refusal) commonly run 5-15%, so a shop whose own history shows 3% would otherwise flag an evening
# with a few more honest retries than usual.
MIN_DECLINE_RATE = 0.08
# Wallet orders younger than this may still be waiting for an honest buyer to log in and approve.
WALLET_APPROVAL_GRACE = timedelta(minutes=2)
# Fewer gaps than this say nothing about regularity: interarrival_cv stays neutral (1.0, Poisson-like).
MIN_GAPS_FOR_CV = 4
# CUSUM reference per one-minute bin (twice the shop's normal chance that a minute holds a decline),
# floored for tiny shops and capped so a busy shop (where most minutes hold one) can still see a slow attack.
CUSUM_K_FLOOR = 0.02
CUSUM_K_CAP = 0.4
SLOW_DECLINE_CATEGORIES = frozenset(
    {DeclineCategory.FRAUD, DeclineCategory.PROBING, DeclineCategory.UNKNOWN_DECLINE}
)
# Refusals that point at stolen or guessed credentials. A generic "do not honor" is the commonest honest
# refusal, so it counts for half in the CUSUM and never as a suspect decline on its own.
SUSPECT_DECLINE_CATEGORIES = frozenset({DeclineCategory.FRAUD, DeclineCategory.PROBING})
UNKNOWN_DECLINE_WEIGHT = 0.5
# A device with this many different card numbers is cycling cards (multi_card_devices).
MULTI_CARD_MIN = 3


@dataclass
class Baseline:
    attempts_per_hour_mean: float = 3.0
    attempts_per_hour_std: float = 2.0
    decline_rate: float = 0.05
    error_rate: float = 0.01
    known_domains: frozenset[str] = field(default_factory=frozenset)
    known_fingerprints: frozenset[str] = field(default_factory=frozenset)
    history_days: int = 0
    # Attempts per hour by UTC hour of day (24 values), from the shop's own history. None: use the mean.
    hourly_rate: tuple[float, ...] | None = None
    # Share of the shop's wallet orders that normally stay unapproved (buyers who change their mind).
    wallet_unapproved_rate: float = 0.0

    def expected_per_hour(self, now: datetime) -> float:
        """Normal attempts per hour at this hour of day, floored (MIN_EXPECTED_PER_HOUR)."""
        rate = self.hourly_rate[now.hour] if self.hourly_rate else self.attempts_per_hour_mean
        return max(rate, MIN_EXPECTED_PER_HOUR)

    def public(self) -> dict[str, Any]:
        return {
            "attempts_per_hour_mean": round(self.attempts_per_hour_mean, 2),
            "attempts_per_hour_std": round(self.attempts_per_hour_std, 2),
            "decline_rate": round(self.decline_rate, 3),
            "error_rate": round(self.error_rate, 3),
            "wallet_unapproved_rate": round(self.wallet_unapproved_rate, 3),
            "known_email_domains": len(self.known_domains),
            "history_days": self.history_days,
        }


@dataclass
class Features:
    window_min: int
    attempts: int = 0
    attempts_per_min: float = 0.0
    # Poisson test of `attempts` against the shop's normal for this hour of day (triggers.velocity_spike).
    expected_attempts: float = 0.0
    velocity_surprise: float = 0.0  # -log10 of P(at least this many attempts | normal)
    reached_paypal: int = 0
    declined: int = 0
    decline_rate: float = 0.0
    # Binomial test of declines against the shop's normal decline rate (triggers.decline_spike).
    decline_surprise: float = 0.0
    fraud_declines: int = 0
    fraud_decline_share: float = 0.0  # of declines, the share PayPal coded as fraud (stolen, restricted)
    errors: int = 0
    error_rate: float = 0.0
    captured: int = 0
    captured_amount: float = 0.0
    blocked_or_challenged: int = 0
    sca_required: int = 0
    unique_fingerprints: int = 0
    top_fingerprint_share: float = 0.0
    unique_cards_ratio: float = 0.0
    unique_payers_ratio: float = 0.0
    top_sku: str | None = None
    top_sku_share: float = 0.0
    cheapest_sku_share: float = 0.0
    # Card cycling on the cheapest item (triggers.cheap_sku_card_cycling). Cards and payers are counted
    # separately and never mixed with devices: a bot rotates device ids for free, not real credentials.
    cheapest_sku_attempts: int = 0
    cheapest_sku_cards: int = 0
    cheapest_sku_payers: int = 0
    cheapest_sku_decline_rate: float = 0.0
    avg_amount: float = 0.0
    unique_email_domains: int = 0
    new_email_domain_share: float = 0.0
    known_customer_share: float = 0.0
    wallet_share: float = 0.0
    # Credentials per device, over devices that used any: a bot cycles cards, a shopper retries one or two.
    cards_per_device_max: int = 0
    cards_per_device_p90: float = 0.0
    multi_card_devices: int = (
        0  # devices with MULTI_CARD_MIN or more card numbers: one fumbler is at most one
    )
    payers_per_device_max: int = 0
    payers_per_device_p90: float = 0.0
    # Wallet orders old enough to have been approved, and the share still waiting (CREATED): logins that
    # never complete are what testing stolen PayPal accounts looks like.
    aged_wallet_orders: int = 0
    unapproved_wallet_share: float = 0.0
    # Coefficient of variation of the gaps between attempts: about 1 for independent shoppers, low for a
    # script on a timer. 1.0 (neutral) with too few gaps to say.
    interarrival_cv: float = 1.0
    # Processor errors: an outage hits many devices and returning customers; one device erroring is not one.
    error_devices: int = 0
    error_top_device_share: float = 0.0
    error_known_share: float = 0.0
    error_top_sku_share: float = 0.0  # an integration bug on one product concentrates errors on its SKU
    # Baskets: a rush buys across the catalogue, a tester hammers one cheap item.
    distinct_skus: int = 0
    sku_entropy: float = 0.0  # Shannon entropy over SKUs, 0 (one item) to 1 (even across the catalogue)
    amount_cv: float = 0.0
    # Decline mix (app/paypal/codes.py categories), as shares of declines; processor errors as a share of
    # all failures (declines + errors).
    probing_decline_share: float = 0.0
    customer_decline_share: float = 0.0
    processor_share: float = 0.0
    # Low-and-slow: fraud, probing or unexplained declines, and their CUSUM over one-minute bins.
    non_customer_declines: int = 0
    slow_decline_cusum: float = 0.0
    # Fraud or probing refusals, and on how many devices: a card tester's refusals come from many attempts,
    # one shopper's mistyped security code from one device.
    suspect_declines: int = 0
    suspect_decline_devices: int = 0
    # Errors a client can cause on purpose (codes.CLIENT_STATE_ERRORS): kept out of every rate here.
    client_errors: int = 0
    decline_codes: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in asdict(self).items()}


def _ratio(a: float, b: float) -> float:
    return a / b if b else 0.0


def _p90(values: list[int]) -> float:
    """Nearest-rank 90th percentile (0 for no values)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return float(ordered[max(0, math.ceil(0.9 * len(ordered)) - 1)])


def _cv(values: list[float]) -> float:
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    if mean <= 0:
        return 0.0
    return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values)) / mean


def _per_device(w: list[AttemptRow], key: str) -> list[int]:
    seen: dict[str, set[str]] = defaultdict(set)
    for r in w:
        if value := getattr(r, key):
            seen[r.fingerprint_hash].add(card_number(value) if key == "card_key" else value)
    return [len(v) for v in seen.values()]


def _client_error(r: AttemptRow) -> bool:
    return r.outcome == Outcome.ERROR and r.decline_code in CLIENT_STATE_ERRORS


def reached_paypal(outcome: str, decline_code: str | None) -> bool:
    """The decline rate's denominator: the guard's own blocks and challenges never reach a bank, and an
    error a client caused on purpose says nothing about PayPal. The War Room and AG Studio both use this."""
    if outcome in {Outcome.BLOCKED, Outcome.CHALLENGED}:
        return False
    return not (outcome == Outcome.ERROR and decline_code in CLIENT_STATE_ERRORS)


def _slow_decline_cusum(w: list[AttemptRow], window_min: int, now: datetime, baseline: Baseline) -> float:
    """CUSUM over one-minute bins of "this minute had a fraud, probing or unexplained decline". A bin counts
    once however many declines it holds (1 for a fraud or probing refusal, UNKNOWN_DECLINE_WEIGHT for only
    generic ones): a burst is the other triggers' job, this one measures persistence. Honest bad luck
    (insufficient funds, expired card) is left out, so a busy sale does not build it up.
    The reference k is twice the shop's normal chance that a minute holds any decline (a Bernoulli rate
    per bin from its attempt and decline rates; the total decline rate over-states the non-customer one,
    which errs towards fewer alarms), floored for tiny shops and capped (CUSUM_K_CAP) so that at a busy
    shop, where most minutes hold a decline, a bin can still grow the sum."""
    bins = [0.0] * window_min
    for r in w:
        if r.outcome == Outcome.DECLINED and r.decline_category in SLOW_DECLINE_CATEGORIES:
            idx = window_min - 1 - min(window_min - 1, int((now - r.ts).total_seconds() // 60))
            weight = 1.0 if r.decline_category in SUSPECT_DECLINE_CATEGORIES else UNKNOWN_DECLINE_WEIGHT
            bins[idx] = max(bins[idx], weight)
    per_min = baseline.expected_per_hour(now) / 60
    p_minute = 1 - math.exp(-per_min * baseline.decline_rate)
    k = min(CUSUM_K_CAP, max(CUSUM_K_FLOOR, 2 * p_minute))
    return stats.cusum(bins, k)


def compute(rows: list[AttemptRow], window_min: int, now: datetime, baseline: Baseline) -> Features:
    start = now - timedelta(minutes=window_min)
    w = [r for r in rows if start < r.ts <= now]
    f = Features(window_min=window_min, attempts=len(w))
    if not w:
        return f

    f.attempts_per_min = len(w) / window_min
    f.expected_attempts = baseline.expected_per_hour(now) * window_min / 60
    f.velocity_surprise = stats.surprise(stats.poisson_sf(len(w), f.expected_attempts))

    # Blocked or challenged never reached PayPal; a client-caused error tells us nothing about PayPal.
    f.client_errors = sum(_client_error(r) for r in w)
    reached = [r for r in w if reached_paypal(r.outcome, r.decline_code)]
    f.reached_paypal = len(reached)
    f.declined = sum(r.outcome == Outcome.DECLINED for r in reached)
    f.errors = sum(r.outcome == Outcome.ERROR for r in reached)
    f.decline_rate = _ratio(f.declined, len(reached))
    p0 = max(baseline.decline_rate, MIN_DECLINE_RATE)
    f.decline_surprise = stats.surprise(stats.binom_sf(f.declined, len(reached), p0))
    f.fraud_declines = sum(
        r.outcome == Outcome.DECLINED and r.decline_category == DeclineCategory.FRAUD for r in reached
    )
    f.fraud_decline_share = _ratio(f.fraud_declines, f.declined)
    f.error_rate = _ratio(f.errors, len(reached))
    captured = [r for r in w if r.outcome in {Outcome.CAPTURED, Outcome.REFUNDED}]
    f.captured = len(captured)
    f.captured_amount = sum(r.amount for r in captured)
    f.blocked_or_challenged = sum(r.outcome in {Outcome.BLOCKED, Outcome.CHALLENGED} for r in w)
    # Stopped by 3-D Secure: a bot's card order left waiting for the bank's check, or a browser card whose
    # check failed or was cancelled. Not a decline (a real shopper meets both), and never flags a device.
    f.sca_required = sum(r.outcome in SCA_STOPPED for r in w)

    fps = Counter(r.fingerprint_hash for r in w)
    f.unique_fingerprints = len(fps)
    f.top_fingerprint_share = fps.most_common(1)[0][1] / len(w)
    f.known_customer_share = _ratio(sum(r.fingerprint_hash in baseline.known_fingerprints for r in w), len(w))

    cards = [r.card_key for r in w if r.card_key]
    f.unique_cards_ratio = _ratio(len(set(cards)), len(cards))
    payers = [r.payer_id_hash for r in w if r.payer_id_hash]
    f.unique_payers_ratio = _ratio(len(set(payers)), len(payers))
    f.wallet_share = _ratio(sum(r.method == "wallet" for r in w), len(w))

    skus = Counter(r.sku for r in w)
    f.top_sku, top_n = skus.most_common(1)[0]
    f.top_sku_share = top_n / len(w)
    f.cheapest_sku_share = skus.get(CHEAPEST_SKU, 0) / len(w)
    cheap = [r for r in w if r.sku == CHEAPEST_SKU]
    cheap_reached = [r for r in reached if r.sku == CHEAPEST_SKU]
    f.cheapest_sku_attempts = len(cheap)
    f.cheapest_sku_cards = len({r.card_key for r in cheap if r.card_key})
    f.cheapest_sku_payers = len({r.payer_id_hash for r in cheap if r.payer_id_hash})
    f.cheapest_sku_decline_rate = _ratio(
        sum(r.outcome == Outcome.DECLINED for r in cheap_reached), len(cheap_reached)
    )
    f.avg_amount = sum(r.amount for r in w) / len(w)

    domains = [r.email_domain for r in w if r.email_domain]
    f.unique_email_domains = len(set(domains))
    f.new_email_domain_share = _ratio(sum(d not in baseline.known_domains for d in domains), len(domains))
    f.decline_codes = dict(Counter(r.decline_code for r in reached if r.decline_code).most_common(6))

    cards_per_device = _per_device(w, "card_key")
    payers_per_device = _per_device(w, "payer_id_hash")
    f.cards_per_device_max = max(cards_per_device, default=0)
    f.cards_per_device_p90 = _p90(cards_per_device)
    f.multi_card_devices = sum(n >= MULTI_CARD_MIN for n in cards_per_device)
    f.payers_per_device_max = max(payers_per_device, default=0)
    f.payers_per_device_p90 = _p90(payers_per_device)

    aged = [r for r in w if r.method == "wallet" and r.ts <= now - WALLET_APPROVAL_GRACE]
    aged = [
        r for r in aged if r.outcome not in {Outcome.BLOCKED, Outcome.CHALLENGED} and not _client_error(r)
    ]
    f.aged_wallet_orders = len(aged)
    f.unapproved_wallet_share = _ratio(sum(r.outcome == Outcome.CREATED for r in aged), len(aged))

    times = sorted(r.ts for r in w)
    gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:], strict=False)]
    f.interarrival_cv = _cv(gaps) if len(gaps) >= MIN_GAPS_FOR_CV else 1.0

    errors = [r for r in reached if r.outcome == Outcome.ERROR]
    error_fps = Counter(r.fingerprint_hash for r in errors)
    f.error_devices = len(error_fps)
    f.error_top_device_share = _ratio(error_fps.most_common(1)[0][1], len(errors)) if errors else 0.0
    known_errors = sum(r.fingerprint_hash in baseline.known_fingerprints for r in errors)
    f.error_known_share = _ratio(known_errors, len(errors))
    error_skus = Counter(r.sku for r in errors)
    f.error_top_sku_share = _ratio(error_skus.most_common(1)[0][1], len(errors)) if errors else 0.0

    f.distinct_skus = len(skus)
    if len(PRODUCTS) > 1:
        entropy = -sum((n / len(w)) * math.log(n / len(w)) for n in skus.values())
        f.sku_entropy = entropy / math.log(len(PRODUCTS))
    f.amount_cv = _cv([r.amount for r in w])

    declines = [r for r in reached if r.outcome == Outcome.DECLINED]
    f.probing_decline_share = _ratio(
        sum(r.decline_category == DeclineCategory.PROBING for r in declines), len(declines)
    )
    f.customer_decline_share = _ratio(
        sum(r.decline_category == DeclineCategory.CUSTOMER for r in declines), len(declines)
    )
    f.processor_share = _ratio(f.errors, f.errors + f.declined)
    f.non_customer_declines = sum(r.decline_category in SLOW_DECLINE_CATEGORIES for r in declines)
    suspect = [r for r in declines if r.decline_category in SUSPECT_DECLINE_CATEGORIES]
    f.suspect_declines = len(suspect)
    f.suspect_decline_devices = len({r.fingerprint_hash for r in suspect})
    f.slow_decline_cusum = _slow_decline_cusum(w, window_min, now, baseline)
    return f


def compute_all(rows: list[AttemptRow], now: datetime, baseline: Baseline) -> dict[int, Features]:
    return {m: compute(rows, m, now, baseline) for m in WINDOWS_MIN}


def lookup(features: dict[int, Features], key: str, window: int) -> float | None:
    """Value the analyst's evidence claims are checked against."""
    f = features.get(window)
    if f is None or key not in FeatureKey.__members__.values():
        return None
    value = getattr(f, key, None)
    return float(value) if isinstance(value, int | float) else None
