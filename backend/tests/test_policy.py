from app.db.models import ActionType
from app.detection.triggers import Tier
from app.policy.engine import PolicyInput, decide, rules_first, rules_hint

FRAUD = frozenset({Tier.FRAUD, Tier.VOLUME})


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
        assert types(decide(inp(classification=cls, confidence=0.70))) == [ActionType.NOTIFY]


def test_r2_below_the_confidence_floor_falls_through_to_r3():
    # It used to notify only at any confidence: a 0.4 "genuine rush" on attack traffic disarmed everything.
    for cls in ("genuine_rush", "paypal_outage"):
        actions = decide(inp(classification=cls, confidence=0.6, trigger_tiers=FRAUD))
        assert types(actions) == [ActionType.SCA_ON, ActionType.NOTIFY]
        assert actions[0].rule == "R3_soft_defence"


def test_relax_restores_the_checkout_when_a_confident_harmless_verdict_follows_r0():
    for cls in ("genuine_rush", "paypal_outage"):
        actions = decide(inp(classification=cls, confidence=0.9, guard_set_by_incident=True))
        assert types(actions) == [ActionType.RELAX, ActionType.NOTIFY]
        assert actions[0].rule == f"R2_relax_{cls}"
    # Nothing of this incident's to undo (another incident's defences, or none): no RELAX.
    assert types(decide(inp(classification="genuine_rush", confidence=0.9))) == [ActionType.NOTIFY]
    # Below the floor there is no RELAX either.
    low = decide(inp(classification="genuine_rush", confidence=0.6, guard_set_by_incident=True))
    assert ActionType.RELAX not in types(low)
    # The kill switch wins over everything.
    assert types(decide(inp(classification="genuine_rush", confidence=0.9, kill_switch=True))) == [
        ActionType.NOTIFY
    ]


def test_r0_fraud_tier_turns_on_3ds_before_the_analyst():
    assert [(a.type, a.rule) for a in rules_first(inp(trigger_tiers=FRAUD))] == [
        (ActionType.SCA_ON, "R0_fraud_signal")
    ]
    assert rules_first(inp(trigger_tiers=frozenset({Tier.VOLUME, Tier.HOLDING, Tier.OPS}))) == []
    assert rules_first(inp(trigger_tiers=FRAUD, sale_window_active=True)) == []  # the owner announced a sale
    assert rules_first(inp(trigger_tiers=FRAUD, kill_switch=True)) == []


def ai_down(**kw) -> PolicyInput:
    return inp(
        **{
            "classification": "unclear",
            "confidence": 0.5,
            "ai_available": False,
            "trigger_tiers": FRAUD,
            "strong_fingerprints": ["fp1"],
        }
        | kw
    )


def test_r3b_ai_down_fraud_blocks_only_strong_fingerprints_and_never_refunds():
    actions = decide(ai_down())
    assert types(actions) == [ActionType.SCA_ON, ActionType.BLOCK_FP, ActionType.HOLD_SKU, ActionType.NOTIFY]
    block = next(a for a in actions if a.type == ActionType.BLOCK_FP)
    assert block.params == {"fingerprints": ["fp1"]}  # not fp2: suspicious, but not strong
    assert all(a.rule == "R3b_ai_down_fraud_signal" for a in actions[:3])
    # No strong fingerprints, no targeted item: 3-D Secure only.
    assert types(decide(ai_down(strong_fingerprints=[], attack_sku=None))) == [
        ActionType.SCA_ON,
        ActionType.NOTIFY,
    ]


def test_r3b_does_not_apply_during_a_sale_with_the_ai_up_or_with_paypal_failing():
    soft = [ActionType.SCA_ON, ActionType.NOTIFY]
    assert types(decide(ai_down(sale_window_active=True))) == soft
    assert types(decide(ai_down(ai_available=True))) == soft  # an AI "unclear" is not an outage
    assert types(decide(ai_down(trigger_tiers=FRAUD | {Tier.OPS}))) == soft


def test_r3c_volume_only_returning_customers_get_no_friction():
    rush = dict(
        classification="unclear",
        confidence=0.5,
        trigger_tiers=frozenset({Tier.VOLUME}),
        known_customer_share=0.7,
        decline_rate=0.04,
    )
    for ai in (True, False):
        actions = decide(inp(**rush, ai_available=ai))
        assert [(a.type, a.rule) for a in actions] == [(ActionType.NOTIFY, "R3c_returning_customer_rush")]
    assert ActionType.SCA_ON in types(decide(inp(**rush | {"known_customer_share": 0.2})))
    assert ActionType.SCA_ON in types(decide(inp(**rush | {"decline_rate": 0.3})))
    assert ActionType.SCA_ON in types(
        decide(inp(**rush | {"trigger_tiers": frozenset({Tier.VOLUME, Tier.FRAUD})}))
    )


def test_r3d_ai_down_and_only_paypal_errors_notifies_only():
    for t in ({Tier.OPS}, {Tier.OPS, Tier.HOLDING}):
        actions = decide(ai_down(trigger_tiers=frozenset(t)))
        assert [(a.type, a.rule) for a in actions] == [
            (ActionType.NOTIFY, "R3d_processor_trouble_notify_only")
        ]
    # With the AI up but unsure, the usual soft defence applies.
    assert types(decide(ai_down(trigger_tiers=frozenset({Tier.OPS}), ai_available=True))) == [
        ActionType.SCA_ON,
        ActionType.NOTIFY,
    ]


def test_rules_hint_is_separate_from_the_classification():
    assert rules_hint(ai_down()) == "fraud_signals"
    assert rules_hint(ai_down(trigger_tiers=frozenset({Tier.OPS}))) == "processor_trouble"
    assert (
        rules_hint(ai_down(trigger_tiers=frozenset({Tier.VOLUME}), known_customer_share=0.8, decline_rate=0))
        == "returning_customer_rush"
    )
    assert rules_hint(ai_down(trigger_tiers=frozenset({Tier.VOLUME}))) == "unusual_volume"
    assert rules_hint(ai_down(trigger_tiers=frozenset())) is None


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
