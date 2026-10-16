from datetime import UTC, datetime, timedelta

from app.ai.analyst import validate_evidence
from app.ai.reporter import fill, has_stray_numbers
from app.ai.schemas import Evidence, Verdict
from app.detection.features import AttemptRow, Baseline, compute_all
from app.detection.triggers import evaluate, signature

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
) -> AttemptRow:
    return AttemptRow(
        ts=NOW - timedelta(seconds=seconds_ago if seconds_ago is not None else i * 2),
        sku=sku,
        amount=3.0 if sku == "SKU-TEALIGHT-3" else 18.0,
        method="card",
        fingerprint_hash=fp,
        email_domain=domain,
        card_key=f"400000{i:04d}",
        payer_id_hash=None,
        outcome=outcome,
        decline_code="INSTRUMENT_DECLINED" if outcome == "declined" else None,
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
