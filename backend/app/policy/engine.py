"""Policy engine: fixed, deterministic rules that decide what Checkout Guard may do.

The classification is an input: the deterministic scorecard's verdict (app/detection/classifier.py), with
Gemini only confirming an unsure one (app/incidents/decision.py: an AI-confirmed verdict is capped below
the refund floor, and in practice below the blocking floor, so it gets R3b). Nothing here moves money or
changes the checkout on a model's say-so, and whether Gemini answered (or only wrote the explanation)
never changes the defences: only the recorded decision does.
Trigger tiers (app/detection/triggers.py) let the rules act on the kind of signal before any verdict.

  R0 fraud-tier trigger fired            → 3-D Secure at once, before the verdict (rules_first);
                                           not during an owner-announced sale, not with the kill switch on
  R1 kill switch on                      → notify only
  R2 genuine rush or PayPal outage,      → notify, and RELAX: undo this incident's 3-D Secure and SKU holds
     confidence ≥ 0.70                     (restore the guard state from before the incident; blocked
                                           fingerprints stay blocked). Below 0.70 it falls through to R3.
                                           An outage call never relaxes while fraud-tier triggers fired in
                                           the incident (errors can be manufactured): it is R3 instead.
  R3 unclear, or confidence < 0.70       → soft defences: 3-D Secure for cards + notify, except
     R3c volume-only triggers, mostly returning customers, few declines → notify only (no friction)
     R3d unopposed tie-break and only ops triggers (PayPal failing)     → notify only
     R3b unopposed tie-break (Gemini agreed with the unsure scorecard, or was down, off or unverifiable),
         fraud-tier trigger, no sale, PayPal not failing →
         3-D Secure, block the strong fingerprints only (several cards or fraud/probing refusals, never a
         known customer), hold the targeted SKU for first-time shoppers. Never refunds. With processor
         errors in the mix, shoppers retrying through the outage blur the picture, so it is plain R3.
         An AI that answered "unclear" or disagreed with the scorecard keeps it at plain R3 (soft only).
  R4 attack, confidence ≥ 0.70           → 3-D Secure, block attack fingerprints, hold the targeted SKU
                                           for first-time shoppers, notify
     … but during an owner-announced sale → 3-D Secure + notify only (no blocking)
  R5 refunds of matched attack captures  → only with confidence ≥ 0.85 (the scorecard alone: an
                                           AI-confirmed verdict is capped below it), outside a sale window;
                                           executed automatically under the caps when autopilot is
                                           on, otherwise queued for owner approval; also queued
                                           while a kept AI-confirmed verdict is stale, so blocks and holds
                                           continue but no new money moves unseen

"unclear" stays "unclear"; what the triggers alone suggest is kept apart as rules_hint(), shown next to it
and never used as a classification (the scorecard supersedes it).
"""

from dataclasses import dataclass, field
from typing import Any

from app.ai.schemas import ATTACK_CLASSES
from app.db.models import ActionType
from app.detection.triggers import Tier

CONFIDENCE_ACT = 0.70
CONFIDENCE_REFUND = 0.85
HARMLESS_CLASSES = frozenset({"genuine_rush", "paypal_outage"})
# R3c: a busy shop of mostly returning customers whose banks say yes is a rush, whatever the AI says.
RUSH_KNOWN_SHARE = 0.50
RUSH_MAX_DECLINE_RATE = 0.10


@dataclass
class PlannedAction:
    type: ActionType
    rule: str
    params: dict[str, Any] = field(default_factory=dict)
    needs_approval: bool = False


@dataclass
class PolicyInput:
    classification: str
    confidence: float
    autopilot: bool
    kill_switch: bool
    sale_window_active: bool
    attack_fingerprints: list[str]
    attack_sku: str | None
    # The verdict is a kept AI-confirmed one that the AI could not re-confirm for a while (cadence.py).
    verdict_stale: bool = False
    # The scorecard was unsure and nobody contradicted it: Gemini's second opinion agreed, or could not be
    # had (down, quota, switched off, evidence that did not match). Set from the recorded decision only
    # (service._policy_input), never from whether Gemini wrote the explanation.
    unopposed_tiebreak: bool = False
    trigger_tiers: frozenset[Tier] = frozenset()
    known_customer_share: float = 0.0  # 15-minute window
    decline_rate: float = 0.0  # 15-minute window
    # Subset of attack_fingerprints that is safe to block on rules alone (Signature.strong_fingerprints).
    strong_fingerprints: list[str] = field(default_factory=list)
    # The guard state was last changed by this incident: there is something RELAX could restore.
    guard_set_by_incident: bool = False


def _volume_only(p: PolicyInput) -> bool:
    return bool(p.trigger_tiers) and p.trigger_tiers <= {Tier.VOLUME}


def _ops_only(p: PolicyInput) -> bool:
    # Holding (blocked_activity) alongside is fine: existing blocks are not a new fraud signal.
    return Tier.OPS in p.trigger_tiers and not p.trigger_tiers & {Tier.FRAUD, Tier.VOLUME}


def _looks_like_rush(p: PolicyInput) -> bool:
    return (
        _volume_only(p)
        and p.known_customer_share >= RUSH_KNOWN_SHARE
        and p.decline_rate <= RUSH_MAX_DECLINE_RATE
    )


def rules_first(p: PolicyInput) -> list[PlannedAction]:
    """R0: applied (and committed) before the verdict, so an unsure scorecard waiting on Gemini's second
    opinion, or an outage, never leaves a card-testing burst undefended. 3-D Secure only: it costs a real
    shopper one bank prompt, and a confident harmless verdict undoes it (RELAX)."""
    if p.kill_switch or p.sale_window_active or Tier.FRAUD not in p.trigger_tiers:
        return []
    return [PlannedAction(ActionType.SCA_ON, "R0_fraud_signal")]


def rules_hint(p: PolicyInput) -> str | None:
    """What the triggers alone suggest. Shown next to an "unclear" verdict; never used as a classification."""
    if Tier.FRAUD in p.trigger_tiers:
        return "fraud_signals"
    if _ops_only(p):
        return "processor_trouble"
    if _looks_like_rush(p):
        return "returning_customer_rush"
    if p.trigger_tiers & {Tier.VOLUME}:
        return "unusual_volume"
    return None


def decide(p: PolicyInput) -> list[PlannedAction]:
    notify = PlannedAction(ActionType.NOTIFY, "always_notify")
    if p.kill_switch:
        return [PlannedAction(ActionType.NOTIFY, "R1_kill_switch")]
    # A bot can make captures fail on purpose (capturing orders nobody approved): an "outage" with fraud
    # signals in the same incident gets the soft defences, never a RELAX.
    fake_outage = p.classification == "paypal_outage" and Tier.FRAUD in p.trigger_tiers
    if p.classification in HARMLESS_CLASSES and p.confidence >= CONFIDENCE_ACT and not fake_outage:
        actions = [PlannedAction(ActionType.NOTIFY, f"R2_{p.classification}")]
        if p.guard_set_by_incident:
            actions.insert(0, PlannedAction(ActionType.RELAX, f"R2_relax_{p.classification}"))
        return actions

    is_attack = p.classification in ATTACK_CLASSES
    if not is_attack or p.confidence < CONFIDENCE_ACT:
        return _rules_only(p, notify)

    if p.sale_window_active:
        return [PlannedAction(ActionType.SCA_ON, "R4_attack_during_sale_soft_only"), notify]

    actions = [PlannedAction(ActionType.SCA_ON, "R4_attack")]
    if p.attack_fingerprints:
        actions.append(
            PlannedAction(ActionType.BLOCK_FP, "R4_attack", {"fingerprints": sorted(p.attack_fingerprints)})
        )
    if p.attack_sku:
        actions.append(PlannedAction(ActionType.HOLD_SKU, "R4_attack", {"sku": p.attack_sku}))
    if p.confidence >= CONFIDENCE_REFUND:
        if p.verdict_stale:
            actions.append(
                PlannedAction(ActionType.REFUND, "R5_stale_verdict_needs_owner", needs_approval=True)
            )
        else:
            actions.append(
                PlannedAction(ActionType.REFUND, "R5_refund_matched", needs_approval=not p.autopilot)
            )
    actions.append(notify)
    return actions


def _rules_only(p: PolicyInput, notify: PlannedAction) -> list[PlannedAction]:
    """R3 family: no confident verdict to act on (unclear, or below the floor)."""
    if _looks_like_rush(p):
        return [PlannedAction(ActionType.NOTIFY, "R3c_returning_customer_rush")]
    if p.unopposed_tiebreak and _ops_only(p):
        # 3-D Secure during a PayPal outage only adds a failing step for shoppers already retrying.
        return [PlannedAction(ActionType.NOTIFY, "R3d_processor_trouble_notify_only")]
    if (
        p.unopposed_tiebreak
        and Tier.FRAUD in p.trigger_tiers
        and Tier.OPS not in p.trigger_tiers
        and not p.sale_window_active
    ):
        actions = [PlannedAction(ActionType.SCA_ON, "R3b_unopposed_fraud_signal")]
        if p.strong_fingerprints:
            actions.append(
                PlannedAction(
                    ActionType.BLOCK_FP,
                    "R3b_unopposed_fraud_signal",
                    {"fingerprints": sorted(p.strong_fingerprints)},
                )
            )
        if p.attack_sku:
            actions.append(
                PlannedAction(ActionType.HOLD_SKU, "R3b_unopposed_fraud_signal", {"sku": p.attack_sku})
            )
        return [*actions, notify]
    return [PlannedAction(ActionType.SCA_ON, "R3_soft_defence"), notify]
