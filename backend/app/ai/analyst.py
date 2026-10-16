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
from app.detection.features import AttemptRow, Baseline, Features, lookup

PROMPT_VERSION = "analyst-v1"
log = structlog.get_logger()

SYSTEM = """You are Checkout Guard's fraud analyst for a small online shop that takes PayPal payments.
A deterministic detector flagged unusual checkout traffic. Decide what it is:

- card_testing: a bot testing stolen cards. Typical signs: many attempts on the cheapest item, high
  decline rate with card-decline codes, a new card on almost every attempt, many email domains never
  seen before, few repeat customers, tiny average amounts.
- account_testing: the same but with stolen PayPal accounts (wallet payments, many unique payer ids).
- genuine_rush: real customers, e.g. an owner-announced sale. Signs: a sale window is active, varied
  items and basket sizes, returning customers and common email domains, low decline rate.
- paypal_outage: PayPal/processor trouble. Signs: processor errors (5xx, timeouts, SERVICE_UNAVAILABLE)
  across all kinds of customers rather than card declines; volume not unusually high.
- unclear: signals conflict or there is too little data.

Rules:
- Cite only features present in the input. For each evidence item copy `observed` exactly from the
  given window (rates are 0-1 fractions, not percentages).
- Confidence reflects how clearly the data separates the classes. Use < 0.7 when unsure.
- Recommend actions; you do not execute anything. Fixed safety rules decide what is allowed.
- The summary is for a non-technical shop owner: short, calm, concrete.
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
    return {
        "triggers": triggers,
        "windows": {f"{m}m": f.as_dict() for m, f in features.items()},
        "baseline": baseline.public(),
        "sale_window_active": sale_window is not None,
        "sale_note": (sale_window or {}).get("note"),
        "guard_state": guard_state,
        "actions_so_far": actions_so_far,
        "sample_attempts": [
            {
                "sku": r.sku,
                "amount": r.amount,
                "method": r.method,
                "outcome": r.outcome,
                "decline_code": r.decline_code,
                "fp": r.fingerprint_hash[:8],
                "email_domain": _clean_domain(r.email_domain),
                "returning_customer": r.fingerprint_hash in baseline.known_fingerprints,
            }
            for r in sorted(sample, key=lambda r: r.ts, reverse=True)[:20]
        ],
    }


def validate_evidence(verdict: Verdict, features: dict[int, Features]) -> tuple[list[Evidence], int]:
    """Keep only evidence whose numbers match what we computed (guards against invented numbers)."""
    kept: list[Evidence] = []
    for ev in verdict.evidence:
        actual = lookup(features, ev.feature, ev.window_min)
        if actual is None:
            continue
        tolerance = max(abs(actual) * 0.05, 0.02)
        if abs(ev.observed - actual) <= tolerance:
            ev.observed = round(actual, 3)
            kept.append(ev)
    return kept, len(verdict.evidence) - len(kept)


def fallback_verdict(reason: str) -> Verdict:
    return Verdict(
        classification="unclear",
        confidence=0.5,
        summary="Unusual checkout activity detected. AI analysis is unavailable, so only soft defences apply.",  # noqa: E501
        evidence=[],
        recommended_actions=["SCA_ON", "NOTIFY"],
        uncertainty_notes=f"AI unavailable: {reason}",
    )


async def analyse(analyst_input: dict[str, Any], features: dict[int, Features]) -> AnalystResult:
    prompt = "Classify this checkout activity.\n\n" + json.dumps(analyst_input, default=str)
    for attempt in range(2):
        try:
            verdict, model_id = await generate_json(system=SYSTEM, prompt=prompt, schema=Verdict)
        except AIUnavailable as exc:
            return AnalystResult(fallback_verdict(str(exc)[:160]), "rules-fallback", ai_available=False)

        kept, dropped = validate_evidence(verdict, features)
        if kept or verdict.classification == "unclear" or attempt == 1:
            verdict.evidence = kept
            if not kept and verdict.classification != "unclear":
                # No verifiable evidence after a retry: don't let an unsupported verdict drive actions.
                verdict.confidence = min(verdict.confidence, 0.6)
                verdict.uncertainty_notes += " Evidence could not be verified against computed features."
            return AnalystResult(verdict, model_id, ai_available=True, dropped_evidence=dropped)
        log.info("analyst_retry_unverified_evidence", dropped=dropped)
        prompt += "\n\nYour previous evidence values did not match the input. Copy `observed` exactly."
    raise AssertionError("unreachable")
