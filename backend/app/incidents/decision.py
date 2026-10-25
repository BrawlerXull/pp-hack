"""Who decides the classification: the scorecard, with Gemini as a tie-breaker only. Pure functions.

- The scorecard (app/detection/classifier.py) always runs and is the primary verdict.
- When it is unsure (classification "unclear", or the margin to the runner-up is under MARGIN_TIEBREAK)
  Gemini is asked for an independent second opinion:
    agrees with the scorecard's top class (with evidence that matched our numbers)
                     → that class, confidence capped at AGREE_CAP and never above the scorecard's own
    disagrees        → "unclear": soft defences only
    unavailable, or a class with no verifiable evidence → the scorecard's own call stands
- Gemini can never pick a class the scorecard did not rank first, never raises a confidence, and never
  decides on its own: an AI answer that matches the scorecard's runner-up is still a disagreement.
"""

from dataclasses import dataclass, field
from typing import Any

from app.ai.schemas import Verdict
from app.detection.classifier import ScoreResult

DECIDED_BY_SCORECARD = "scorecard"
DECIDED_BY_AGREE = "scorecard+gemini_agree"
DECIDED_BY_DISAGREE = "gemini_disagree_unclear"
# An agreed tie-break never reaches the refund floor (policy CONFIDENCE_REFUND): money only moves on a
# verdict the scorecard is sure of by itself. A tie-break means a margin under MARGIN_TIEBREAK, so the top
# probability is under 0.65 and an agreement stays below the blocking floor too: policy R3b applies.
AGREE_CAP = 0.80
# Under classifier.MIN_ATTEMPTS the scorecard's probabilities rest on a handful of rows: an AI agreeing
# with them stays below the blocking floor (policy CONFIDENCE_ACT = 0.70).
LOW_DATA_CAP = 0.65


@dataclass
class Decision:
    classification: str
    confidence: float
    decided_by: str
    # Set when Gemini was asked as tie-breaker: {"outcome": agree|disagree|unavailable|unverified,
    # "gemini": its classification or None, "model": model id or None}.
    tiebreak: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)


def scorecard_only(score: ScoreResult) -> Decision:
    return Decision(score.classification, score.confidence, DECIDED_BY_SCORECARD)


def combine(
    score: ScoreResult, gemini: Verdict | None, *, model_id: str | None = None, verified: bool = True
) -> Decision:
    """The tie-break. `gemini` is None when the AI was unavailable (down, quota, switched off);
    `verified`: at least one of its evidence items matched our computed numbers."""
    if not score.needs_tiebreak:
        return scorecard_only(score)
    # An "unclear" answer needs no evidence to count as a disagreement; a class does to count at all.
    if gemini is None or (not verified and gemini.classification != "unclear"):
        outcome = "unavailable" if gemini is None else "unverified"
        d = scorecard_only(score)
        d.tiebreak = {
            "outcome": outcome,
            "gemini": gemini.classification if gemini else None,
            "model": model_id,
        }
        return d
    tiebreak = {"outcome": "agree", "gemini": gemini.classification, "model": model_id}
    if gemini.classification == score.top_class:
        cap = LOW_DATA_CAP if score.too_little_data else AGREE_CAP
        return Decision(score.top_class, min(score.confidence, cap), DECIDED_BY_AGREE, tiebreak=tiebreak)
    tiebreak["outcome"] = "disagree"
    return Decision("unclear", score.confidence, DECIDED_BY_DISAGREE, tiebreak=tiebreak)


def unopposed_tiebreak(incident: Any) -> bool:
    """The recorded decision was an unsure scorecard that nobody contradicted: Gemini agreed, or its second
    opinion could not be had or verified. Policy R3b/R3d key on this, so whether Gemini answered a later
    explanation call never changes the defences."""
    if incident.decided_by == DECIDED_BY_AGREE:
        return True
    tiebreak = (incident.scorecard or {}).get("tiebreak") or {}
    return incident.decided_by == DECIDED_BY_SCORECARD and tiebreak.get("outcome") in (
        "unavailable",
        "unverified",
    )
