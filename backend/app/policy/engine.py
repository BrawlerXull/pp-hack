"""Policy engine: fixed, deterministic rules that decide what Checkout Guard may do.

The AI's verdict is an input; it never moves money or changes the checkout on its own.

  R1 kill switch on                      → notify only
  R2 genuine rush or PayPal outage       → notify only (never punish real customers)
  R3 unclear, or confidence < 0.70       → soft defences: 3-D Secure for cards + notify
  R4 attack, confidence ≥ 0.70           → 3-D Secure, block attack fingerprints, hold the targeted SKU
                                           for first-time shoppers, notify
     … but during an owner-announced sale → 3-D Secure + notify only (no blocking)
  R5 refunds of matched attack captures  → only with confidence ≥ 0.85, outside a sale window;
                                           executed automatically under the caps when autopilot is
                                           on, otherwise queued for owner approval
"""

from dataclasses import dataclass, field
from typing import Any

from app.ai.schemas import ATTACK_CLASSES
from app.db.models import ActionType

CONFIDENCE_ACT = 0.70
CONFIDENCE_REFUND = 0.85


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


def decide(p: PolicyInput) -> list[PlannedAction]:
    notify = PlannedAction(ActionType.NOTIFY, "always_notify")
    if p.kill_switch:
        return [PlannedAction(ActionType.NOTIFY, "R1_kill_switch")]
    if p.classification in {"genuine_rush", "paypal_outage"}:
        return [PlannedAction(ActionType.NOTIFY, f"R2_{p.classification}")]

    is_attack = p.classification in ATTACK_CLASSES
    if not is_attack or p.confidence < CONFIDENCE_ACT:
        return [PlannedAction(ActionType.SCA_ON, "R3_soft_defence"), notify]

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
        actions.append(PlannedAction(ActionType.REFUND, "R5_refund_matched", needs_approval=not p.autopilot))
    actions.append(notify)
    return actions
