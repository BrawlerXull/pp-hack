from app.db.models import ActionType
from app.policy.engine import PolicyInput, decide


def inp(**kw) -> PolicyInput:
    base = dict(
        classification="card_testing",
        confidence=0.95,
        autopilot=True,
        kill_switch=False,
        sale_window_active=False,
        attack_fingerprints=["fp1", "fp2"],
        attack_sku="SKU-TEALIGHT-3",
    )
    return PolicyInput(**(base | kw))


def types(actions) -> list[ActionType]:
    return [a.type for a in actions]


def test_confident_attack_gets_full_response():
    actions = decide(inp())
    assert types(actions) == [
        ActionType.SCA_ON,
        ActionType.BLOCK_FP,
        ActionType.HOLD_SKU,
        ActionType.REFUND,
        ActionType.NOTIFY,
    ]
    refund = next(a for a in actions if a.type == ActionType.REFUND)
    assert not refund.needs_approval


def test_kill_switch_only_notifies():
    assert types(decide(inp(kill_switch=True))) == [ActionType.NOTIFY]


def test_genuine_rush_and_outage_never_block_or_refund():
    for cls in ("genuine_rush", "paypal_outage"):
        assert types(decide(inp(classification=cls, confidence=0.99))) == [ActionType.NOTIFY]


def test_low_confidence_or_unclear_is_soft_only():
    assert types(decide(inp(confidence=0.6))) == [ActionType.SCA_ON, ActionType.NOTIFY]
    assert types(decide(inp(classification="unclear", confidence=0.9))) == [
        ActionType.SCA_ON,
        ActionType.NOTIFY,
    ]


def test_attack_during_sale_window_is_soft_only():
    assert types(decide(inp(sale_window_active=True))) == [ActionType.SCA_ON, ActionType.NOTIFY]


def test_refunds_need_high_confidence():
    assert ActionType.REFUND not in types(decide(inp(confidence=0.8)))


def test_autopilot_off_queues_refunds_for_approval():
    refund = next(a for a in decide(inp(autopilot=False)) if a.type == ActionType.REFUND)
    assert refund.needs_approval


def test_stale_verdict_keeps_blocks_but_queues_refunds_for_the_owner():
    actions = decide(inp(verdict_stale=True))
    assert types(actions) == [
        ActionType.SCA_ON,
        ActionType.BLOCK_FP,
        ActionType.HOLD_SKU,
        ActionType.REFUND,
        ActionType.NOTIFY,
    ]
    refund = next(a for a in actions if a.type == ActionType.REFUND)
    assert refund.needs_approval and refund.rule == "R5_stale_verdict_needs_owner"
