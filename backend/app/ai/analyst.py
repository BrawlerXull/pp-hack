"""AI analyst: explains the scorecard's decision, and gives a second opinion when the scorecard is unsure.

The classification is decided by app/detection/classifier.py (deterministic). Gemini is used for:
  explain()  the owner-facing summary of a decision already made (its own reading is recorded, never used)
  analyse()  an independent second opinion when the scorecard says "unclear" or its margin is small; it
             is not shown the scorecard's answer, and app/incidents/decision.combine decides what an
             agreement or a disagreement is worth (it can only confirm, never upgrade)
Privacy: the model only ever sees aggregated features and a few masked rows (email domain,
8-char fingerprint prefix, SKU, amount, outcome). No card numbers, names, emails, tokens or credentials.
Whatever it says is advice; app/policy/engine.py decides what actually happens.
"""

import json
import re
from dataclasses import dataclass
from typing import Any

import structlog

from app.ai.gemini import AI_DISABLED, AIUnavailable, generate_json
from app.ai.schemas import ATTACK_CLASSES, Evidence, Verdict
from app.config import get_settings
from app.detection.classifier import ScoreResult
from app.detection.features import WINDOWS_MIN, AttemptRow, Baseline, FeatureKey, Features, lookup

PROMPT_VERSION = "analyst-v4"
log = structlog.get_logger()

# Second opinion (analyse): the model classifies on its own, without seeing the scorecard's answer.
SYSTEM = """You are Checkout Guard's fraud analyst for a small online shop that takes PayPal payments.
A deterministic detector flagged unusual checkout traffic. Decide what it is from the traffic pattern:

- card_testing: a bot testing stolen cards. Typical signs: many attempts on the cheapest item, high
  decline rate, declines PayPal codes as fraud (fraud_declines, fraud_decline_share: stolen, lost,
  restricted cards) or as guessing (wrong CVV/expiry), a new card on almost every attempt
  (cheapest_sku_cards), many email domains never seen before, few repeat customers, tiny amounts.
- account_testing: the same but with stolen PayPal accounts (wallet payments, several PayPal accounts
  per device (payers_per_device_max), wallet orders never approved (unapproved_wallet_share),
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

# Explanation (explain): the decision is made; the model only puts it into words for the owner.
EXPLAIN_SYSTEM = """You explain a decision that Checkout Guard's deterministic scorecard has already made
about unusual checkout traffic at a small online shop that takes PayPal payments. You do not decide
anything and you cannot change the decision.

The input has `decision` (the classification, the scorecard's confidence and its main reasons, with
numbers from our own data) and the traffic it was decided from.

Write for a non-technical shop owner:
- summary: one or two short, calm sentences on what is happening and why the scorecard thinks so.
- evidence: two to four of the most telling features from the input. Copy `observed` exactly from the
  given window (rates are 0-1 fractions, not percentages).
- uncertainty_notes: anything in the traffic that points the other way, or an empty string.
- Set `classification` and `confidence` exactly to the decision's values.
- Never write digits or numbers in `summary`, in an evidence `why` or in `uncertainty_notes`: describe
  the pattern in words. The real numbers are shown next to your text from our own data.
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
    # At least one evidence item matched our numbers (a second opinion without any does not count).
    verified: bool = True
    # explain(): the class the model itself named; recorded in the audit log, never used.
    gemini_view: str | None = None
    # explain(): the model answered, but its text was replaced by Checkout Guard's template (it argued for
    # another class, or misread our numbers). The words must not be credited to the model.
    template_text: bool = False


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


RECOMMENDED: dict[str, list[str]] = {
    "card_testing": ["SCA_ON", "BLOCK_FP", "HOLD_SKU", "REFUND_MATCHED", "NOTIFY"],
    "account_testing": ["SCA_ON", "BLOCK_FP", "HOLD_SKU", "REFUND_MATCHED", "NOTIFY"],
    "genuine_rush": ["NOTIFY"],
    "paypal_outage": ["NOTIFY"],
    "unclear": ["SCA_ON", "NOTIFY"],
}


def template_verdict(
    classification: str, confidence: float, score: ScoreResult, notes: list[str] | None = None
) -> Verdict:
    """The verdict text when Gemini does not write it: our own wording around the scorecard's reasons.
    No recommendations: a fixed list is not advice anyone gave (the reporter would print "recommended
    only" next to it)."""
    evidence = score.evidence()
    summary = deterministic_summary(classification, evidence)
    if classification == "unclear" and score.top_class:
        summary += f" The scorecard leaned towards {score.top_class.replace('_', ' ')}."
    return Verdict(
        classification=classification,
        confidence=round(confidence, 3),
        summary=summary,
        evidence=evidence,
        recommended_actions=[],
        uncertainty_notes=" ".join(notes or []),
    )


def fallback_verdict(reason: str) -> Verdict:
    return Verdict(
        classification="unclear",
        confidence=0.5,
        summary="Unusual checkout activity detected. AI analysis is unavailable, so fixed safety rules apply.",  # noqa: E501
        evidence=[],
        recommended_actions=["SCA_ON", "NOTIFY"],
        uncertainty_notes="AI analysis is switched off (AI_ENABLED=false)."
        if reason == AI_DISABLED
        else f"AI unavailable ({reason}). It will retry on the next assessment.",
    )


def _fallback(reason: str) -> AnalystResult:
    return AnalystResult(
        fallback_verdict(reason), "rules-fallback", ai_available=False, fallback_reason=reason
    )


async def analyse(analyst_input: dict[str, Any], features: dict[int, Features]) -> AnalystResult:
    """Independent second opinion (the tie-break). The input never contains the scorecard's answer."""
    if not get_settings().ai_enabled:
        return _fallback(AI_DISABLED)  # switched off on purpose: no call, no quota; the scorecard decides
    prompt = "Classify this checkout activity.\n\n" + json.dumps(analyst_input, default=str)
    for attempt in range(2):
        try:
            verdict, model_id = await generate_json(system=SYSTEM, prompt=prompt, schema=Verdict)
        except AIUnavailable as exc:
            return _fallback(_friendly(exc))

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
                verdict,
                model_id,
                ai_available=True,
                dropped_evidence=dropped,
                numbers_replaced=replaced,
                verified=bool(kept),
            )
        log.info("analyst_retry_unverified_evidence", dropped=dropped)
        prompt += "\n\nYour previous evidence values did not match the input. Copy `observed` exactly."
    raise AssertionError("unreachable")


async def explain(
    analyst_input: dict[str, Any],
    features: dict[int, Features],
    score: ScoreResult,
    classification: str,
    confidence: float,
) -> AnalystResult:
    """Owner-facing words for a decision already made. The returned verdict always carries the decided
    classification and confidence; the model's text is kept only if it argues for that decision, cites
    at least one number we computed (when it cites any) and passes the number guard."""
    if not get_settings().ai_enabled:
        return _fallback(AI_DISABLED)
    decision = {
        "classification": classification,
        "confidence": round(confidence, 3),
        "reasons": [
            {
                "feature": r.feature,
                "window_min": r.window_min,
                "observed": round(r.observed, 3),
                "why": r.text,
            }
            for r in score.top_reasons()
            if r.feature is not None and r.observed is not None
        ],
    }
    prompt = "Explain this decision.\n\n" + json.dumps(analyst_input | {"decision": decision}, default=str)
    try:
        verdict, model_id = await generate_json(system=EXPLAIN_SYSTEM, prompt=prompt, schema=Verdict)
    except AIUnavailable as exc:
        return _fallback(_friendly(exc))

    view = verdict.classification
    kept, dropped = validate_evidence(verdict, features)
    verdict.classification, verdict.confidence = classification, round(confidence, 3)
    verdict.evidence = kept or score.evidence()
    replaced = number_guard(verdict, features)
    off_topic = view != classification and not (view in ATTACK_CLASSES and classification in ATTACK_CLASSES)
    template_text = off_topic or (dropped and not kept)
    # Recommendations only ever for the decided class: a model reading another class must not have its
    # advice (refunds for what we called real customers) printed in the owner's report.
    allowed = RECOMMENDED.get(classification, [])
    verdict.recommended_actions = [a for a in verdict.recommended_actions if a in allowed]
    if template_text:
        # Text that argues for another class, or reads our numbers wrongly, is not an explanation of this
        # decision: use our own wording, no advice and no trend from it, and say so.
        verdict.summary = deterministic_summary(classification, score.evidence())
        verdict.uncertainty_notes = (
            "The AI read the traffic differently, so this explanation is Checkout Guard's own wording."
            if off_topic
            else "The AI's numbers did not match the computed data, so this is Checkout Guard's own wording."
        )
        verdict.recommended_actions = []
        verdict.trend = "stable"
        replaced += 1
    return AnalystResult(
        verdict,
        model_id,
        ai_available=True,
        dropped_evidence=dropped,
        numbers_replaced=replaced,
        verified=bool(kept),
        gemini_view=view,
        template_text=template_text,
    )
