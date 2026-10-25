"""Scorecard classifier: decides what an incident is from our own computed features. Pure: no I/O, no AI.

Why not the LLM: a Gemini call takes 3-25 s, fails on free-tier quota, answers differently to the same
input and reads attacker-controlled text. A scorecard is instant, deterministic, explainable line by line
and cannot be talked into anything. Gemini now writes the owner-facing explanation and is asked for a
second opinion only when the scorecard is unsure (app/incidents/service.py).

How it works: each class (card_testing, account_testing, genuine_rush, paypal_outage) has a short list of
transparent rules. A rule that holds adds (or subtracts) its points. The class scores go through a softmax
with TEMPERATURE to give per-class probabilities ("calibrated-ish": the temperature was tuned on our
simulator so that a clear-cut scenario lands above 0.85 and a mixed one well below, not fitted to real
outcomes). The verdict is "unclear" when the top probability is under MIN_TOP_PROB or the margin to the
runner-up is under MARGIN_UNCLEAR. Every reason cites a FeatureKey with OUR computed value, so the
evidence passes app/ai/analyst.validate_evidence by construction.

Guard rails:
  - Supporting context (an owner-announced sale, returning-customer share) can add at most as many points
    as the class's own traffic evidence: it is never the sole reason for a class.
  - Cold start (under COLD_START_DAYS of history): new email domains and few returning customers are
    not attack evidence, since every customer is new to a new shop.
  - Too little data (under MIN_ATTEMPTS attempts) is "unclear", whatever the rules say.
  - One shopper is never an attack: refusal-mix and card-cycling rules count only when the signal spans
    several devices (a shopper mistyping a security code, or trying three of their own cards, is one
    device), and generic refusals ("do not honor", the commonest honest one) are never a core signal.
    A card_testing verdict whose evidence is not spread over several devices is capped at SPREAD_CAP,
    below the policy's refund floor.
The weights are tuned on our own simulator (scripts/eval_detection.py, docs/eval-detection.md); nothing
here has seen a real attack.
"""

import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from app.ai.schemas import Evidence
from app.detection.features import Baseline, FeatureKey, Features, lookup

CLASSIFIER_VERSION = "scorecard-v1"
CLASSES = ("card_testing", "account_testing", "genuine_rush", "paypal_outage")
TEMPERATURE = 1.5
MIN_TOP_PROB = 0.55
MARGIN_UNCLEAR = 0.15
# Below this margin (or "unclear") Gemini is asked for a second opinion (service.py).
MARGIN_TIEBREAK = 0.30
MIN_ATTEMPTS = 5
# The 15-minute window is used when it holds this many attempts; otherwise the hour (a slow attack).
WINDOW_MIN_ATTEMPTS = 8
COLD_START_DAYS = 7
TOP_REASONS = 5
# Fraud or probing refusals must come from at least this many devices to count (Context.suspect_spread).
SPREAD_MIN_DEVICES = 2
# One device with this many card numbers is cycling cards; fewer is an honest retry (Context.card_cycling).
CYCLING_ONE_DEVICE = 5
# card_testing without evidence spread over several devices (SPREAD_STRONG_DEVICES devices with fraud or
# probing refusals, two card-cycling devices, or one far past an honest retry) is capped here, below the
# policy's refund floor (CONFIDENCE_REFUND = 0.85): one shopper's retries never move money automatically.
SPREAD_CAP = 0.80
SPREAD_STRONG_DEVICES = 3

# A rule's test gets the feature's value and the context; it returns whether the rule holds.
Test = Callable[[float, "Context"], bool]


@dataclass(frozen=True)
class Context:
    f: Features
    baseline: Baseline
    sale_active: bool

    @property
    def card_attempts(self) -> float:
        return self.f.attempts * (1 - self.f.wallet_share)

    @property
    def wallet_attempts(self) -> float:
        return self.f.attempts * self.f.wallet_share

    @property
    def suspect_spread(self) -> bool:
        """Fraud or probing refusals from several devices, not one shopper's typos."""
        return self.f.suspect_decline_devices >= SPREAD_MIN_DEVICES

    @property
    def card_cycling(self) -> bool:
        """Card numbers cycled on one device far beyond an honest retry, or on several devices."""
        return self.f.cards_per_device_max >= CYCLING_ONE_DEVICE or self.f.multi_card_devices >= 2


@dataclass(frozen=True)
class Rule:
    cls: str
    feature: FeatureKey
    test: Test
    points: float
    text: str  # plain English, no digits: the observed value is shown next to it from our data
    supporting: bool = False  # context only: capped by the class's traffic evidence
    novelty: bool = False  # "strangers": not attack evidence on a cold start
    # A credential or refusal signal. An attack class with none of its core rules holding is capped at
    # CORELESS_CAP: a busy cheap item, new faces and regular timing alone are not an attack.
    core: bool = False


K = FeatureKey
CT, AT, GR, PO = CLASSES


def _gaps(c: Context) -> bool:
    return c.f.attempts >= 5  # interarrival_cv needs a few gaps (features.MIN_GAPS_FOR_CV)


RULES: tuple[Rule, ...] = (
    # ---- card_testing: stolen cards tried on the cheapest item ------------------------------------------
    Rule(
        CT,
        K.DECLINE_RATE,
        lambda v, c: v >= 0.30 and c.suspect_spread,
        2.0,
        "Most payments that reached PayPal were refused.",
        core=True,
    ),
    Rule(
        CT,
        K.DECLINE_RATE,
        lambda v, c: 0.15 <= v < 0.30 and c.suspect_spread,
        1.0,
        "Many payments were refused.",
        core=True,
    ),
    # Generic refusals alone: worth a look, not an attack (banks say "do not honor" to honest shoppers too).
    Rule(
        CT,
        K.DECLINE_RATE,
        lambda v, c: v >= 0.15 and not c.suspect_spread,
        0.5,
        "Many payments were refused, though not with stolen-card or guessing codes.",
    ),
    Rule(
        CT,
        K.FRAUD_DECLINE_SHARE,
        lambda v, c: v >= 0.25 and c.f.declined >= 2 and c.suspect_spread,
        1.5,
        "Banks coded refusals as stolen, lost or restricted cards.",
        core=True,
    ),
    Rule(
        CT,
        K.PROBING_DECLINE_SHARE,
        lambda v, c: v >= 0.15 and c.f.declined >= 2 and c.suspect_spread,
        1.0,
        "Refusals for a wrong security code or expiry: someone guessing card details.",
        core=True,
    ),
    Rule(CT, K.CHEAPEST_SKU_SHARE, lambda v, c: v >= 0.6, 1.5, "Attempts piled onto the cheapest item."),
    Rule(
        CT,
        K.CARDS_PER_DEVICE_MAX,
        lambda v, c: c.card_cycling,
        2.0,
        "Devices cycled through many different cards.",
        core=True,
    ),
    Rule(
        CT,
        K.CARDS_PER_DEVICE_MAX,
        lambda v, c: v >= 2 and not c.card_cycling,
        0.5,
        "A device tried more than one card.",
    ),
    Rule(
        CT,
        K.UNIQUE_CARDS_RATIO,
        lambda v, c: v >= 0.9 and c.card_attempts >= 8,
        0.5,
        "A new card on almost every attempt.",
    ),
    Rule(
        CT,
        K.INTERARRIVAL_CV,
        lambda v, c: v <= 0.5 and _gaps(c),
        1.0,
        "Attempts arrived at machine-regular intervals.",
    ),
    Rule(CT, K.SKU_ENTROPY, lambda v, c: v <= 0.2 and c.f.attempts >= 5, 0.5, "Every basket was the same."),
    Rule(
        CT,
        K.NEW_EMAIL_DOMAIN_SHARE,
        lambda v, c: v >= 0.5,
        1.0,
        "Most email domains were never seen at this shop.",
        novelty=True,
    ),
    Rule(
        CT,
        K.KNOWN_CUSTOMER_SHARE,
        lambda v, c: v <= 0.1,
        0.5,
        "Hardly any returning customers.",
        supporting=True,
        novelty=True,
    ),
    Rule(CT, K.WALLET_SHARE, lambda v, c: v <= 0.3, 0.5, "Paid by card rather than PayPal wallet."),
    Rule(CT, K.ERROR_RATE, lambda v, c: v >= 0.3, -2.0, "Failures are processor errors, not refusals."),
    Rule(
        CT,
        K.CUSTOMER_DECLINE_SHARE,
        lambda v, c: v >= 0.8 and c.f.declined >= 2,
        -1.5,
        "Refusals are honest bad luck (insufficient funds, expired card).",
    ),
    Rule(CT, K.WALLET_SHARE, lambda v, c: v >= 0.7, -2.0, "Paid by PayPal wallet, not by card."),
    # ---- account_testing: stolen PayPal logins ---------------------------------------------------------
    Rule(AT, K.WALLET_SHARE, lambda v, c: v >= 0.7, 2.0, "Nearly all attempts used PayPal wallet logins."),
    Rule(
        AT,
        K.PAYERS_PER_DEVICE_MAX,
        lambda v, c: v >= 3,
        2.0,
        "One device used several PayPal accounts.",
        core=True,
    ),
    Rule(AT, K.PAYERS_PER_DEVICE_MAX, lambda v, c: v == 2, 0.75, "A device used a second PayPal account."),
    Rule(
        AT,
        K.UNAPPROVED_WALLET_SHARE,
        lambda v, c: c.f.aged_wallet_orders >= 5 and v >= c.baseline.wallet_unapproved_rate + 0.4,
        2.5,
        "Most wallet orders were never approved: logins that do not complete.",
        core=True,
    ),
    Rule(
        AT,
        K.UNIQUE_PAYERS_RATIO,
        lambda v, c: v >= 0.9 and c.wallet_attempts >= 8,
        0.5,
        "A different PayPal account on almost every attempt.",
    ),
    Rule(
        AT,
        K.DECLINE_RATE,
        lambda v, c: v >= 0.15 and c.suspect_spread,
        1.0,
        "Many payments were refused.",
        core=True,
    ),
    Rule(
        AT,
        K.FRAUD_DECLINE_SHARE,
        lambda v, c: v >= 0.25 and c.f.declined >= 2 and c.suspect_spread,
        1.0,
        "Refusals for restricted or locked accounts.",
        core=True,
    ),
    Rule(AT, K.CHEAPEST_SKU_SHARE, lambda v, c: v >= 0.6, 1.5, "Attempts piled onto the cheapest item."),
    Rule(
        AT,
        K.INTERARRIVAL_CV,
        lambda v, c: v <= 0.5 and _gaps(c),
        1.0,
        "Attempts arrived at machine-regular intervals.",
    ),
    Rule(
        AT,
        K.NEW_EMAIL_DOMAIN_SHARE,
        lambda v, c: v >= 0.5,
        1.0,
        "Most email domains were never seen at this shop.",
        novelty=True,
    ),
    Rule(
        AT,
        K.KNOWN_CUSTOMER_SHARE,
        lambda v, c: v <= 0.1,
        0.5,
        "Hardly any returning customers.",
        supporting=True,
        novelty=True,
    ),
    Rule(AT, K.WALLET_SHARE, lambda v, c: v <= 0.3, -3.0, "Paid by card, not by PayPal wallet."),
    Rule(
        AT,
        K.CUSTOMER_DECLINE_SHARE,
        lambda v, c: v >= 0.8 and c.f.declined >= 2,
        -1.5,
        "Refusals are honest bad luck (insufficient funds, expired card).",
    ),
    Rule(AT, K.ERROR_RATE, lambda v, c: v >= 0.3, -2.0, "Failures are processor errors, not refusals."),
    # ---- genuine_rush: real customers at once ----------------------------------------------------------
    Rule(
        GR,
        K.DECLINE_RATE,
        lambda v, c: v <= 0.10 and c.f.reached_paypal >= 5 and c.f.error_rate < 0.15,
        2.0,
        "Banks approved nearly every payment.",
    ),
    Rule(GR, K.SKU_ENTROPY, lambda v, c: v >= 0.5, 1.5, "Baskets were spread across the catalogue."),
    Rule(GR, K.AMOUNT_CV, lambda v, c: v >= 0.3, 0.5, "Order amounts varied."),
    Rule(
        GR,
        K.INTERARRIVAL_CV,
        lambda v, c: v >= 0.7 and _gaps(c),
        1.0,
        "Arrivals were irregular, like independent shoppers.",
    ),
    Rule(
        GR,
        K.CARDS_PER_DEVICE_MAX,
        lambda v, c: not c.card_cycling and c.f.multi_card_devices <= 1 and c.f.payers_per_device_max <= 2,
        1.0,
        "Each shopper used one card or account (at most an honest retry or two).",
    ),
    Rule(
        GR,
        K.FRAUD_DECLINE_SHARE,
        lambda v, c: v <= 0.1,
        0.5,
        "No refusals coded as stolen cards.",
    ),
    Rule(
        GR,
        K.KNOWN_CUSTOMER_SHARE,
        lambda v, c: v >= 0.4,
        1.5,
        "Many returning customers.",
        supporting=True,
    ),
    Rule(GR, K.ERROR_RATE, lambda v, c: v <= 0.1, 0.5, "PayPal was processing normally."),
    Rule(GR, K.DECLINE_RATE, lambda v, c: v >= 0.3, -2.5, "Most payments were refused."),
    Rule(GR, K.FRAUD_DECLINES, lambda v, c: v >= 3, -2.0, "Several refusals coded as stolen cards."),
    Rule(GR, K.CARDS_PER_DEVICE_MAX, lambda v, c: c.card_cycling, -2.0, "Devices cycled through many cards."),
    Rule(GR, K.PAYERS_PER_DEVICE_MAX, lambda v, c: v >= 3, -2.0, "One device used several PayPal accounts."),
    Rule(GR, K.ERROR_RATE, lambda v, c: v >= 0.3, -3.0, "Many payments failed with processor errors."),
    Rule(
        GR,
        K.UNAPPROVED_WALLET_SHARE,
        lambda v, c: c.f.aged_wallet_orders >= 5 and v >= c.baseline.wallet_unapproved_rate + 0.4,
        -1.5,
        "Most wallet orders were never approved.",
    ),
    Rule(
        GR,
        K.CHEAPEST_SKU_SHARE,
        lambda v, c: v >= 0.9 and c.f.attempts >= 10,
        -1.0,
        "Nearly everything was the cheapest item.",
    ),
    # ---- paypal_outage: PayPal or the processor failing -------------------------------------------------
    Rule(PO, K.ERROR_RATE, lambda v, c: v >= 0.3, 3.0, "Many payments failed with processor errors."),
    Rule(PO, K.ERROR_RATE, lambda v, c: 0.15 <= v < 0.3, 1.5, "Some payments failed with processor errors."),
    Rule(
        PO,
        K.PROCESSOR_SHARE,
        lambda v, c: v >= 0.6 and c.f.errors >= 3,
        1.5,
        "Failures were processor errors rather than card refusals.",
    ),
    Rule(PO, K.ERROR_DEVICES, lambda v, c: v >= 3, 1.0, "Errors hit many different shoppers."),
    Rule(
        PO,
        K.ERROR_TOP_DEVICE_SHARE,
        lambda v, c: v <= 0.5 and c.f.errors >= 3,
        0.5,
        "No single device accounts for the errors.",
    ),
    Rule(
        PO,
        K.ERROR_KNOWN_SHARE,
        lambda v, c: v >= 0.4,
        0.5,
        "Returning customers were hit too.",
        supporting=True,
    ),
    Rule(PO, K.DECLINE_RATE, lambda v, c: v <= 0.15, 0.5, "Banks were not refusing cards."),
    Rule(PO, K.ERROR_RATE, lambda v, c: v < 0.1, -3.0, "Hardly any processor errors."),
    Rule(
        PO,
        K.ERROR_TOP_DEVICE_SHARE,
        lambda v, c: v >= 0.7 and c.f.errors >= 3,
        -1.0,
        "The errors come from one device.",
    ),
)
SALE_POINTS = 1.0  # genuine_rush, supporting: the owner announced a sale
CORELESS_CAP = 1.5
CORE_CLASSES = frozenset({CT, AT})


@dataclass
class Reason:
    cls: str
    feature: str | None  # None: context that is not a feature (the sale window)
    window_min: int
    observed: float | None
    points: float
    text: str
    supporting: bool = False


@dataclass
class ScoreResult:
    version: str
    classification: str  # one of CLASSES, or "unclear"
    confidence: float  # the top class's probability (also when the verdict is "unclear")
    top_class: str
    runner_up: str
    margin: float
    probabilities: dict[str, float]
    scores: dict[str, float]
    window_min: int
    reasons: list[Reason] = field(default_factory=list)  # every rule that held, all classes
    notes: list[str] = field(default_factory=list)
    # Under MIN_ATTEMPTS attempts: "unclear" whatever the rules said (decision.combine caps an AI agreement).
    too_little_data: bool = False

    @property
    def needs_tiebreak(self) -> bool:
        return self.classification == "unclear" or self.margin < MARGIN_TIEBREAK

    def top_reasons(self, cls: str | None = None, n: int = TOP_REASONS) -> list[Reason]:
        """The strongest positive reasons for a class (default: the top class)."""
        cls = cls or self.top_class
        mine = [r for r in self.reasons if r.cls == cls and r.points > 0]
        return sorted(mine, key=lambda r: (r.supporting, -r.points))[:n]

    def evidence(self, n: int = TOP_REASONS) -> list[Evidence]:
        """Top reasons as validated-by-construction evidence items (feature reasons only)."""
        return [
            Evidence(
                feature=r.feature,
                window_min=r.window_min,
                observed=round(r.observed, 3),
                why=r.text,
            )
            for r in self.top_reasons(n=n + 1)
            if r.feature is not None and r.observed is not None
        ][:n]

    def public(self) -> dict[str, Any]:
        """What the incident stores and the dashboard shows."""
        return {
            "version": self.version,
            "classification": self.classification,
            "confidence": round(self.confidence, 3),
            "top_class": self.top_class,
            "runner_up": self.runner_up,
            "margin": round(self.margin, 3),
            "probabilities": {k: round(v, 3) for k, v in self.probabilities.items()},
            "scores": {k: round(v, 2) for k, v in self.scores.items()},
            "window_min": self.window_min,
            "top_reasons": [asdict(r) for r in self.top_reasons()],
            "notes": self.notes,
        }


_CORE = {(r.cls, r.feature.value, r.text) for r in RULES if r.core}


def _is_core(r: Reason) -> bool:
    return (r.cls, r.feature, r.text) in _CORE


def softmax(scores: dict[str, float], temperature: float = TEMPERATURE) -> dict[str, float]:
    top = max(scores.values())
    exps = {k: math.exp((v - top) / temperature) for k, v in scores.items()}
    total = sum(exps.values())
    return {k: v / total for k, v in exps.items()}


def pick_window(features: dict[int, Features]) -> int:
    return 15 if features[15].attempts >= WINDOW_MIN_ATTEMPTS else 60


def classify(features: dict[int, Features], baseline: Baseline, *, sale_active: bool) -> ScoreResult:
    window = pick_window(features)
    f = features[window]
    ctx = Context(f, baseline, sale_active)
    cold = baseline.history_days < COLD_START_DAYS
    notes: list[str] = []
    if cold:
        notes.append(
            "cold start: new email domains and few returning customers are not counted as attack signs"
        )

    reasons: list[Reason] = []
    for rule in RULES:
        if cold and rule.novelty:
            continue
        value = lookup(features, rule.feature, window)
        if value is None or not rule.test(value, ctx):
            continue
        reasons.append(
            Reason(rule.cls, rule.feature.value, window, value, rule.points, rule.text, rule.supporting)
        )
    if sale_active:
        reasons.append(Reason(GR, None, window, None, SALE_POINTS, "The owner announced a sale.", True))

    scores = {}
    for cls in CLASSES:
        mine = [r for r in reasons if r.cls == cls]
        evidence = sum(r.points for r in mine if not r.supporting and r.points > 0)
        negative = sum(r.points for r in mine if r.points < 0)
        support = sum(r.points for r in mine if r.supporting and r.points > 0)
        if support > evidence:
            notes.append(f"{cls}: supporting context capped at the traffic evidence")
        positive = evidence + min(support, evidence)
        if cls in CORE_CLASSES and not any(r.points > 0 and _is_core(r) for r in mine):
            if positive > CORELESS_CAP:
                notes.append(f"{cls}: no card, account or refusal signal, capped")
            positive = min(positive, CORELESS_CAP)
        scores[cls] = positive + negative

    probs = softmax(scores)
    ranked = sorted(CLASSES, key=lambda c: probs[c], reverse=True)
    top, second = ranked[0], ranked[1]
    margin = probs[top] - probs[second]
    too_little = f.attempts < MIN_ATTEMPTS
    if too_little:
        notes.append("too little data to classify")
        classification = "unclear"
    elif probs[top] < MIN_TOP_PROB or margin < MARGIN_UNCLEAR:
        classification = "unclear"
    else:
        classification = top
    confidence = probs[top]
    spread = (
        f.suspect_decline_devices >= SPREAD_STRONG_DEVICES
        or f.multi_card_devices >= 2
        or f.cards_per_device_max >= CYCLING_ONE_DEVICE
    )
    if top == CT and not spread and confidence > SPREAD_CAP:
        notes.append("card_testing: evidence from too few devices, confidence capped below the refund floor")
        confidence = SPREAD_CAP
    return ScoreResult(
        version=CLASSIFIER_VERSION,
        classification=classification,
        confidence=confidence,
        top_class=top,
        runner_up=second,
        margin=margin,
        probabilities=probs,
        scores=scores,
        window_min=window,
        reasons=reasons,
        notes=notes,
        too_little_data=too_little,
    )
