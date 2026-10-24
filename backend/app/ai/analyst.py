"""AI analyst: decides whether an anomaly is an attack, a genuine rush or a PayPal outage.

Privacy: the model only ever sees aggregated features and a few masked rows (email domain,
8-char fingerprint prefix, SKU, amount, outcome). No card numbers, names, emails, tokens or credentials.
The verdict is advice; app/policy/engine.py decides what actually happens.
"""

import json
import re
from dataclasses import dataclass
from typing import Any

import structlog

from app.ai.gemini import AIUnavailable, generate_json
from app.ai.schemas import Evidence, Verdict
from app.detection.features import WINDOWS_MIN, AttemptRow, Baseline, FeatureKey, Features, lookup

PROMPT_VERSION = "analyst-v3"
log = structlog.get_logger()

SYSTEM = """You are Checkout Guard's fraud analyst for a small online shop that takes PayPal payments.
A deterministic detector flagged unusual checkout traffic. Decide what it is from the traffic pattern:

- card_testing: a bot testing stolen cards. Typical signs: many attempts on the cheapest item, high
  decline rate, declines PayPal codes as fraud (fraud_declines, fraud_decline_share: stolen, lost,
  restricted cards) or as guessing (wrong CVV/expiry), a new card on almost every attempt
  (cheapest_sku_cards), many email domains never seen before, few repeat customers, tiny amounts.
- account_testing: the same but with stolen PayPal accounts (wallet payments, many unique payer ids,
  restricted-account declines).
- genuine_rush: real customers arriving at once. Signs: varied items and basket sizes, returning
  customers (known_customer_share) and common email domains, a low decline rate whose declines are
  honest bad luck (insufficient funds, expired card) rather than fraud codes. Rushes are often
  unannounced (a post going viral), so judge the pattern itself.
- paypal_outage: PayPal/processor trouble. Signs: processor errors (5xx, timeouts, SERVICE_UNAVAILABLE)
  across all kinds of customers rather than card declines; volume not unusually high.
- unclear: signals conflict or there is too little data.

Supporting context, not proof: `context.sale_window_active` says the owner announced a sale. It makes a
rush more likely, but its absence does not make one an attack, and attackers also strike during sales.

Rules:
- Cite only features present in the input. For each evidence item copy `observed` exactly from the
  given window (rates are 0-1 fractions, not percentages).
- Confidence reflects how clearly the data separates the classes. Use < 0.7 when unsure.
- Recommend actions; you do not execute anything. Fixed safety rules decide what is allowed.
- The summary is for a non-technical shop owner: short, calm, concrete.
- Never write digits or numbers in `summary`, in an evidence `why` or in `uncertainty_notes`: describe
  the pattern in words ("most payments were declined"). The real numbers are shown next to your text
  from our own data.
"""

_SAFE = re.compile(r"[^a-z0-9.\-]")


def _clean_domain(d: str | None) -> str | None:
    # Email domains are attacker-controlled text: strip to a safe charset so they can't carry instructions.
    return _SAFE.sub("", d.lower())[:64] if d else None


@dataclass
class AnalystResult:
    verdict: Verdict
    model_id: str
    ai_available: bool
    dropped_evidence: int = 0
    # Texts whose numbers matched no computed feature and were replaced or cut (number_guard).
    numbers_replaced: int = 0
    # Owner-facing reason the AI could not answer (quota, timeout…), with ai_available=False.
    fallback_reason: str | None = None


def build_input(
    *,
    features: dict[int, Features],
    baseline: Baseline,
    triggers: list[str],
    sale_window: dict[str, Any] | None,
    guard_state: dict[str, Any],
    actions_so_far: list[str],
    sample: list[AttemptRow],
) -> dict[str, Any]:
    # Pattern signals first; the owner's sale window and our own defences come last, as context, so the
    # model judges the traffic before it reads "there's a sale on" (a rush nobody announced must still read
    # as a rush; scripts/eval_ai.py flash_sale_no_hint).
    return {
        "triggers": triggers,
        "windows": {f"{m}m": f.as_dict() for m, f in features.items()},
        "baseline": baseline.public(),
        "sample_attempts": [
            {
                "sku": r.sku,
                "amount": r.amount,
                "method": r.method,
                "outcome": r.outcome,
                "decline_code": r.decline_code,
                "decline_category": r.decline_category,
                "fp": r.fingerprint_hash[:8],
                "email_domain": _clean_domain(r.email_domain),
                "returning_customer": r.fingerprint_hash in baseline.known_fingerprints,
            }
            for r in sorted(sample, key=lambda r: r.ts, reverse=True)[:20]
        ],
        "context": {
            "sale_window_active": sale_window is not None,
            "sale_note": (sale_window or {}).get("note"),
            "guard_state": guard_state,
            "actions_so_far": actions_so_far,
        },
    }


def _tolerance(actual: float) -> float:
    return max(abs(actual) * 0.05, 0.02)  # same tolerance for evidence values and numbers in prose


def validate_evidence(verdict: Verdict, features: dict[int, Features]) -> tuple[list[Evidence], int]:
    """Keep only evidence whose numbers match what we computed (guards against invented numbers)."""
    kept: list[Evidence] = []
    for ev in verdict.evidence:
        actual = lookup(features, ev.feature, ev.window_min)
        if actual is None:
            continue
        if abs(ev.observed - actual) <= _tolerance(actual):
            ev.observed = round(actual, 3)
            kept.append(ev)
    return kept, len(verdict.evidence) - len(kept)


# ---- number guard: AI prose reaches the owner's report ({{ai_summary}}) and the dashboard -------------

# "3-D Secure" / "3DS" are names, not counts. Thousands separators are dropped before matching.
_NAMES = re.compile(r"3-?D(?:S\b|\s+Secure)", re.IGNORECASE)
_NUMBER = re.compile(r"(\d+(?:\.\d+)?)\s*(%|percent\b|per\s*cent\b|min(?:ute)?s?\b)?", re.IGNORECASE)


def _feature_values(features: dict[int, Features]) -> list[float]:
    return [v for m in features for k in FeatureKey if (v := lookup(features, k, m)) is not None]


# Spelled-out amounts from ten up cannot be checked against a feature, so they count as unmatched ("one
# device", "two cards" stay: everyday wording, and too small to mislead).
_NUMBER_WORDS = re.compile(
    r"\b(ten|eleven|twelve|(?:thir|four|fif|six|seven|eigh|nine)teen|twenty|thirty|forty|fifty|sixty|"
    r"seventy|eighty|ninety|hundreds?|thousands?|dozens?)\b",
    re.IGNORECASE,
)


def numbers_match(text: str, values: list[float]) -> bool:
    """True when every number in `text` is one of `values` (within the evidence tolerance). A percentage
    matches only as a 0-1 fraction (every rate feature is one); "15 min" may name a feature window."""
    t = re.sub(r"(?<=\d),(?=\d{3})", "", _NAMES.sub("", text))
    if _NUMBER_WORDS.search(t):
        return False
    for m in _NUMBER.finditer(t):
        n, unit = float(m.group(1)), (m.group(2) or "").lower()
        if unit.startswith("min") and n in WINDOWS_MIN:
            continue
        candidate = n / 100 if unit and not unit.startswith("min") else n
        if not any(abs(candidate - v) <= _tolerance(v) for v in values):
            return False
    return True


FEATURE_TEXT = {k.value: k.value.replace("_", " ") for k in FeatureKey}
SUMMARY_LEAD = {
    "card_testing": "This looks like bots testing stolen cards at your checkout.",
    "account_testing": "This looks like stolen PayPal accounts being tested at your checkout.",
    "genuine_rush": "This looks like a genuine rush of customers.",
    "paypal_outage": "This looks like a problem on PayPal's side rather than fraud.",
    "unclear": "The signals are mixed, so this is not clearly an attack or a rush.",
}


def _fmt(feature: str, value: float) -> str:
    if feature.endswith(("_rate", "_share", "_ratio")):
        return f"{value * 100:.0f}%"
    return f"{value:g}"


def evidence_sentence(ev: Evidence) -> str:
    """Deterministic wording for one validated evidence item: its number is our computed value."""
    value = _fmt(ev.feature, ev.observed)
    return f"{FEATURE_TEXT[ev.feature].capitalize()} was {value} over {ev.window_min} min."


def deterministic_summary(classification: str, evidence: list[Evidence]) -> str:
    signals = "; ".join(
        f"{FEATURE_TEXT[ev.feature]} {_fmt(ev.feature, ev.observed)} over {ev.window_min} min"
        for ev in evidence[:3]
    )
    return SUMMARY_LEAD[classification] + (f" Main signals: {signals}." if signals else "")


def number_guard(verdict: Verdict, features: dict[int, Features]) -> int:
    """The prompt forbids digits in prose, but a model can still write "45 attempts" that we never saw.
    Numbers that match a computed feature pass; otherwise the summary and each `why` are replaced with
    a sentence built from validated evidence, and sentences of the uncertainty notes are cut. Run after
    validate_evidence. Returns how many texts were changed."""
    values = _feature_values(features)
    changed = 0
    if not numbers_match(verdict.summary, values):
        verdict.summary = deterministic_summary(verdict.classification, verdict.evidence)
        changed += 1
    for ev in verdict.evidence:
        if not numbers_match(ev.why, [ev.observed, *values]):
            ev.why = evidence_sentence(ev)
            changed += 1
    sentences = (
        re.split(r"(?<=[.!?])\s+", verdict.uncertainty_notes.strip()) if verdict.uncertainty_notes else []
    )
    kept = [s for s in sentences if numbers_match(s, values)]
    if len(kept) != len(sentences):
        kept.append("Numbers the AI wrote that did not match the computed data were removed.")
        verdict.uncertainty_notes = " ".join(kept)
        changed += 1
    return changed


def _friendly(exc: Exception) -> str:
    if reason := getattr(exc, "reason", None):  # set by app/ai/gemini.py from the breaker's classification
        return reason
    msg = str(exc)
    if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
        return "Gemini quota exhausted"
    if "503" in msg or "UNAVAILABLE" in msg:
        return "Gemini temporarily overloaded"
    if "timed out" in msg.lower() or "TimeoutError" in msg:
        return "Gemini timed out"
    if "GEMINI_API_KEY" in msg:
        return "no Gemini API key configured"
    return "Gemini request failed"


def fallback_verdict(reason: str) -> Verdict:
    return Verdict(
        classification="unclear",
        confidence=0.5,
        summary="Unusual checkout activity detected. AI analysis is unavailable, so fixed safety rules apply.",  # noqa: E501
        evidence=[],
        recommended_actions=["SCA_ON", "NOTIFY"],
        uncertainty_notes=f"AI unavailable ({reason}). It will retry on the next assessment.",
    )


async def analyse(analyst_input: dict[str, Any], features: dict[int, Features]) -> AnalystResult:
    prompt = "Classify this checkout activity.\n\n" + json.dumps(analyst_input, default=str)
    for attempt in range(2):
        try:
            verdict, model_id = await generate_json(system=SYSTEM, prompt=prompt, schema=Verdict)
        except AIUnavailable as exc:
            reason = _friendly(exc)
            return AnalystResult(
                fallback_verdict(reason), "rules-fallback", ai_available=False, fallback_reason=reason
            )

        kept, dropped = validate_evidence(verdict, features)
        if kept or verdict.classification == "unclear" or attempt == 1:
            verdict.evidence = kept
            replaced = number_guard(verdict, features)
            if replaced:
                log.info("analyst_numbers_replaced", texts=replaced)
            if not kept and verdict.classification != "unclear":
                # No verifiable evidence after a retry: don't let an unsupported verdict drive actions.
                verdict.confidence = min(verdict.confidence, 0.6)
                verdict.uncertainty_notes += " Evidence could not be verified against computed features."
            return AnalystResult(
                verdict, model_id, ai_available=True, dropped_evidence=dropped, numbers_replaced=replaced
            )
        log.info("analyst_retry_unverified_evidence", dropped=dropped)
        prompt += "\n\nYour previous evidence values did not match the input. Copy `observed` exactly."
    raise AssertionError("unreachable")
