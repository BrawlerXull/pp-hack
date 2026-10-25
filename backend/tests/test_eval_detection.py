"""P9: the offline detection eval (scripts/eval_detection.py) on a few seeds, as invariants. Simulated traffic
and simulator-tuned weights: these pin down behaviour we rely on, they do not prove real-world accuracy."""

from collections import Counter

import pytest

from app.simulator.scenarios import account_testing, mixed_traffic, stealth_bot
from scripts import eval_detection as ev

SEEDS = 3
ATTACKS = {"card_testing", "account_testing"}


@pytest.fixture(scope="module")
def results() -> dict[str, ev.CaseSummary]:
    return {s.case.name: s for s in ev.evaluate_all(SEEDS)}


def test_the_eval_covers_at_least_eight_scenarios_including_hard_cases():
    names = {c.name for c in ev.CASES}
    assert len(names) >= 8
    assert {"flash_sale_unannounced", "attack_during_sale", "stealth_bot", "mixed_traffic"} <= names


@pytest.mark.parametrize(
    "name",
    [
        "flash_sale",
        "flash_sale_unannounced",
        "normal_night",
        "mixed_traffic",
        "cheap_item_rush",
        "cheap_item_rush_fumbler",
    ],
)
def test_benign_traffic_never_fires_fraud_triggers_or_gets_an_attack_verdict(results, name):
    s = results[name]
    assert s.fraud_rate == 0.0, [sorted(r.triggers) for r in s.runs]
    assert s.attack_calls() == 0, [(r.first, r.final) for r in s.runs]


def test_normal_night_does_not_even_trigger(results):
    assert results["normal_night"].recall == 0.0


@pytest.mark.parametrize("name", ["card_testing", "slow_drip", "stealth_bot", "attack_during_sale"])
def test_card_testing_variants_are_detected_and_called_card_testing(results, name):
    s = results[name]
    assert s.recall == 1.0 and s.fraud_rate == 1.0
    assert all(r.final == "card_testing" for r in s.runs), [(r.first, r.final) for r in s.runs]


def test_account_testing_is_detected(results):
    s = results["account_testing"]
    assert s.recall == 1.0 and all(r.final == "account_testing" for r in s.runs)


def test_outage_is_classified_paypal_outage(results):
    s = results["paypal_outage"]
    assert s.fraud_rate == 0.0 and all(r.final == "paypal_outage" for r in s.runs)


def test_low_and_slow_is_caught_by_slow_declines_alone(results):
    s = results["stealth_bot"]
    assert all(r.triggers == {"slow_declines"} for r in s.runs)
    assert s.median_detect is not None and s.median_detect < 60 * 60


def test_markdown_table_reports_every_case(results):
    table = ev.markdown(list(results.values()), SEEDS)
    for name in results:
        assert f"| {name} |" in table
    assert "Confusion matrix" in table


# ---- the scenarios the eval relies on (all SIMULATED) ---------------------------------------------------


def test_account_testing_is_mostly_unapproved_wallet_logins():
    outcomes = Counter(a.outcome for a in account_testing("offline", count=200, seed=1))
    assert outcomes["pending"] > 0.6 * 200 and outcomes["approve"] < 0.2 * 200
    rows = ev.rows_for(ev.Case("x", ("account_testing",), frozenset(), benign=False), seed=1)
    assert Counter(r.outcome for r in rows)["created"] > len(rows) / 2  # stays CREATED in the shop flow


def test_stealth_bot_rotates_devices_and_waits_minutes():
    attempts = list(stealth_bot("offline", seed=1))
    assert len({a.device_id for a in attempts}) == len(attempts)
    assert all(60 <= a.delay_s <= 300 for a in attempts) and all(a.is_bot for a in attempts)


def test_mixed_traffic_has_a_few_honest_second_card_retries():
    attempts = list(mixed_traffic("offline", count=100, seed=3))
    by_device: dict[str, list] = {}
    for a in attempts:
        by_device.setdefault(a.device_id + a.email, []).append(a)
    retries = [v for v in by_device.values() if [a.outcome for a in v][:2] == ["decline", "approve"]]
    assert retries and all(v[0].card_number != v[1].card_number for v in retries)
    # Honest refusals: mostly the bank's generic "do not honor", some insufficient funds (never a stolen-card
    # or guessing code).
    refusals = Counter(a.card_name for a in attempts if a.outcome == "decline")
    assert set(refusals) <= {"CCREJECT-REFUSED", "CCREJECT-IF"} and refusals["CCREJECT-REFUSED"] > 0


def test_one_fumbling_shopper_in_a_rush_is_never_an_attack(results):
    """Two wrong security codes and two more cards from one device, in a rush of first-time shoppers on
    the cheapest item: no fraud-tier trigger, no attack verdict at any point (so no blocks or refunds)."""
    s = results["cheap_item_rush_fumbler"]
    assert s.fraud_rate == 0.0 and s.attack_calls() == 0
    rows = ev.rows_for(next(c for c in ev.CASES if c.name == "cheap_item_rush_fumbler"), seed=1)
    fumbles = [r for r in rows if r.decline_code and "CVV2" in r.decline_code]
    assert len(fumbles) == 2 and len({r.fingerprint_hash for r in fumbles}) == 1
