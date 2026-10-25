from datetime import UTC, datetime, timedelta

from app.ai.analyst import validate_evidence
from app.ai.reporter import fill, has_stray_numbers
from app.ai.schemas import Evidence, Verdict
from app.detection.features import AttemptRow, Baseline, compute_all, lookup
from app.detection.triggers import FRAUD_TRIGGERS, TRIGGER_TIER, Tier, evaluate, signature, tiers

NOW = datetime(2026, 10, 16, 2, 30, tzinfo=UTC)
KNOWN = frozenset({"cust-a", "cust-b"})
BASE = Baseline(known_domains=frozenset({"gmail.com"}), known_fingerprints=KNOWN)


def row(
    i: int,
    *,
    fp: str,
    sku: str = "SKU-TEALIGHT-3",
    outcome: str = "declined",
    domain: str = "x.top",
    seconds_ago: int | None = None,
    category: str | None = None,
    card: str | None = None,
    payer: str | None = None,
) -> AttemptRow:
    return AttemptRow(
        ts=NOW - timedelta(seconds=seconds_ago if seconds_ago is not None else i * 2),
        sku=sku,
        amount=3.0 if sku == "SKU-TEALIGHT-3" else 18.0,
        method="card",
        fingerprint_hash=fp,
        email_domain=domain,
        card_key=card if card is not None else f"400000{i:04d}",
        payer_id_hash=payer,
        outcome=outcome,
        decline_code="INSTRUMENT_DECLINED" if outcome == "declined" else None,
        decline_category=category if category else ("unknown_decline" if outcome == "declined" else None),
    )


def attack_rows(n: int = 60) -> list[AttemptRow]:
    return [
        row(i, fp=f"bot{i % 3}", outcome="captured" if i % 4 == 0 else "declined", domain=f"m{i}.top")
        for i in range(n)
    ]


def test_card_testing_burst_fires_triggers_and_signature():
    rows = attack_rows()
    feats = compute_all(rows, NOW, BASE)
    fired = evaluate(feats, BASE)
    assert {"velocity_spike", "decline_spike", "sku_concentration"} <= set(fired)
    assert feats[5].cheapest_sku_share == 1.0
    assert feats[5].unique_cards_ratio == 1.0

    sig = signature(rows, NOW - timedelta(minutes=15), NOW, BASE)
    assert sig.top_sku == "SKU-TEALIGHT-3"
    assert sig.fingerprints == ["bot0", "bot1", "bot2"]
    assert len(sig.new_email_domains) == 60


def test_quiet_shop_fires_nothing():
    rows = [
        row(i, fp="cust-a", sku="SKU-PILLAR-18", outcome="captured", domain="gmail.com", seconds_ago=i * 900)
        for i in range(3)
    ]
    assert evaluate(compute_all(rows, NOW, BASE), BASE) == []


def test_returning_customers_are_never_in_the_attack_signature():
    rows = attack_rows() + [row(i, fp="cust-a", outcome="declined", domain="gmail.com") for i in range(5)]
    sig = signature(rows, NOW - timedelta(minutes=15), NOW, BASE)
    assert "cust-a" not in sig.fingerprints


def test_evidence_with_invented_numbers_is_dropped():
    feats = compute_all(attack_rows(), NOW, BASE)
    real = feats[5].decline_rate
    v = Verdict(
        classification="card_testing",
        confidence=0.9,
        summary="s",
        recommended_actions=["SCA_ON"],
        evidence=[
            Evidence(feature="decline_rate", window_min=5, observed=real, why="real"),
            Evidence(feature="decline_rate", window_min=5, observed=0.12, why="made up"),
            Evidence(feature="attempts", window_min=60, observed=999, why="made up"),
        ],
    )
    kept, dropped = validate_evidence(v, feats)
    assert [e.why for e in kept] == ["real"] and dropped == 2


def test_report_placeholders_and_number_guard():
    assert (
        fill(
            "{{refunded_count}} refunded ({{refunded_total}})",
            {"refunded_count": "6", "refunded_total": "$18.00"},
        )
        == "6 refunded ($18.00)"
    )
    assert not has_stray_numbers("Turned on 3-D Secure; refunded {{refunded_count}} payments.\n1. First step")
    assert has_stray_numbers("I refunded 6 payments.")


# ---- fraud-tier triggers (report items 3, 22) and tiers (item 14) ---------------------------------------


def test_every_trigger_has_a_tier_and_sku_concentration_is_volume():
    rows = attack_rows()
    assert set(evaluate(compute_all(rows, NOW, BASE), BASE)) <= set(TRIGGER_TIER)
    assert TRIGGER_TIER["sku_concentration"] == Tier.VOLUME
    assert FRAUD_TRIGGERS == {"decline_spike", "fraud_declines", "cheap_sku_card_cycling", "slow_declines"}
    assert tiers(["velocity_spike", "processor_errors", "nonsense"]) == {Tier.VOLUME, Tier.OPS}


def test_three_fraud_coded_declines_in_ten_minutes_fire_fraud_declines():
    def rows(category: str, n: int) -> list[AttemptRow]:
        # Spread over 9 minutes on different devices and items: nothing else should fire.
        return [
            row(i, fp=f"d{i}", sku="SKU-PILLAR-18", category=category, seconds_ago=i * 180) for i in range(n)
        ]

    feats = compute_all(rows("fraud", 3), NOW, BASE)
    assert feats[10].fraud_declines == 3 and feats[10].fraud_decline_share == 1.0
    assert evaluate(feats, BASE) == ["fraud_declines"]
    assert evaluate(compute_all(rows("fraud", 2), NOW, BASE), BASE) == []
    # Honest bad luck (insufficient funds) is a decline but not a fraud signal.
    assert "fraud_declines" not in evaluate(compute_all(rows("customer", 5), NOW, BASE), BASE)
    # Older than 10 minutes: out of the window.
    old = [row(i, fp=f"d{i}", category="fraud", seconds_ago=660 + i) for i in range(3)]
    assert compute_all(old, NOW, BASE)[10].fraud_declines == 0


def test_fraud_decline_features_are_citable_evidence():
    feats = compute_all([row(i, fp="b", category="fraud") for i in range(4)], NOW, BASE)
    assert lookup(feats, "fraud_declines", 10) == 4.0 and lookup(feats, "fraud_decline_share", 10) == 1.0
    v = Verdict(
        classification="card_testing",
        confidence=0.9,
        summary="s",
        recommended_actions=["SCA_ON"],
        evidence=[
            Evidence(feature="fraud_declines", window_min=10, observed=4, why="real"),
            Evidence(feature="fraud_decline_share", window_min=10, observed=0.2, why="made up"),
        ],
    )
    kept, dropped = validate_evidence(v, feats)
    assert [e.why for e in kept] == ["real"] and dropped == 1


def cycling(n: int, *, declined_every: int, cards: bool = True, payers: bool = False, devices: bool = False):
    """n attempts on the cheapest item from one device (or n devices) within 15 min."""
    return [
        row(
            i,
            fp=f"dev{i}" if devices else "bot",
            outcome="declined" if i % declined_every == 0 else "captured",
            card=f"card{i}" if cards else "",
            payer=f"payer{i}" if payers else None,
            seconds_ago=i * 60,
        )
        for i in range(n)
    ]


def test_cheap_sku_card_cycling_needs_volume_credentials_and_declines():
    assert "cheap_sku_card_cycling" in evaluate(compute_all(cycling(10, declined_every=3), NOW, BASE), BASE)
    # Stolen PayPal accounts count the same way as cards.
    wallet = cycling(10, declined_every=3, cards=False, payers=True)
    assert "cheap_sku_card_cycling" in evaluate(compute_all(wallet, NOW, BASE), BASE)
    # A sale on the cheap item: many cards, but the banks say yes.
    assert "cheap_sku_card_cycling" not in evaluate(
        compute_all(cycling(12, declined_every=5), NOW, BASE), BASE
    )
    # Too few tries.
    assert "cheap_sku_card_cycling" not in evaluate(
        compute_all(cycling(9, declined_every=2), NOW, BASE), BASE
    )


def test_cheap_sku_card_cycling_never_counts_devices_as_credentials():
    # Ten devices, one card, no payers: rotating device ids is free, so it is not card cycling.
    rows = cycling(10, declined_every=2, cards=False, devices=True)
    f15 = compute_all(rows, NOW, BASE)[15]
    assert f15.unique_fingerprints == 10 and f15.cheapest_sku_cards == 0 and f15.cheapest_sku_payers == 0
    assert "cheap_sku_card_cycling" not in evaluate(compute_all(rows, NOW, BASE), BASE)


def test_strong_fingerprints_need_several_cards_or_guessing_declines_and_are_never_known():
    rows = [
        row(1, fp="cards", card="c1", outcome="captured"),
        row(2, fp="cards", card="c2", outcome="captured"),
        row(3, fp="probe", card="p", category="probing"),
        row(4, fp="probe", card="p", category="fraud"),
        row(5, fp="unlucky", card="u", category="customer"),
        row(6, fp="unlucky", card="u", category="customer"),
        row(7, fp="cust-a", card="k1", category="fraud"),
        row(8, fp="cust-a", card="k2", category="fraud"),
    ]
    sig = signature(rows, NOW - timedelta(minutes=15), NOW, BASE)
    assert sig.strong_fingerprints == ["cards", "probe"]
    # Two honest "insufficient funds" refusals are bad luck, not an attack: never blocked; a capture from
    # it would be a weak match (owner approval only).
    assert "unlucky" not in sig.fingerprints and sig.weak_fingerprints == ["unlucky"]
