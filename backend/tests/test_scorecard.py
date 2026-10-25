"""P9: the deterministic scorecard, its features, the statistical triggers and the Gemini tie-break rules.
Pure: no database, no network."""

import math
from datetime import UTC, datetime, timedelta

import pytest

from app.ai import analyst
from app.ai.schemas import Evidence, Verdict
from app.detection import classifier, stats
from app.detection import features as features_mod
from app.detection.features import AttemptRow, Baseline, compute, compute_all
from app.detection.triggers import evaluate
from app.incidents.decision import (
    AGREE_CAP,
    DECIDED_BY_AGREE,
    DECIDED_BY_DISAGREE,
    DECIDED_BY_SCORECARD,
    LOW_DATA_CAP,
    combine,
    unopposed_tiebreak,
)
from app.shop.catalog import CHEAPEST_SKU, PRODUCTS

NOW = datetime(2026, 10, 20, 19, 0, tzinfo=UTC)
KNOWN = frozenset(f"cust{i}" for i in range(20))
BASE = Baseline(
    attempts_per_hour_mean=0.4,
    decline_rate=0.03,
    known_domains=frozenset({"gmail.com"}),
    known_fingerprints=KNOWN,
    history_days=28,
)
SKUS = list(PRODUCTS)


def row(
    seconds_ago: float,
    *,
    fp: str = "bot",
    sku: str = CHEAPEST_SKU,
    outcome: str = "captured",
    category: str | None = None,
    card: str | None = "c",
    payer: str | None = None,
    method: str = "card",
    domain: str = "gmail.com",
) -> AttemptRow:
    return AttemptRow(
        ts=NOW - timedelta(seconds=seconds_ago),
        sku=sku,
        amount=float(PRODUCTS[sku].price),
        method=method,
        fingerprint_hash=fp,
        email_domain=domain,
        card_key=card,
        payer_id_hash=payer,
        outcome=outcome,
        decline_code="X" if outcome in ("declined", "error") else None,
        decline_category=category,
    )


# ---- stats ---------------------------------------------------------------------------------------------


def test_poisson_and_binomial_tails_match_known_values():
    assert stats.poisson_sf(0, 3.0) == 1.0
    assert stats.poisson_sf(1, 2.0) == pytest.approx(1 - math.exp(-2.0))
    assert stats.poisson_sf(3, 1.0) == pytest.approx(1 - math.exp(-1) * (1 + 1 + 0.5))
    assert stats.poisson_sf(200, 1.0) < 1e-300 or stats.poisson_sf(200, 1.0) == 0.0  # far tail, no overflow
    assert stats.binom_sf(1, 1, 0.3) == pytest.approx(0.3)
    assert stats.binom_sf(2, 3, 0.5) == pytest.approx(0.5)
    assert stats.binom_sf(0, 10, 0.1) == 1.0 and stats.binom_sf(11, 10, 0.1) == 0.0
    assert stats.surprise(1e-4) == pytest.approx(4.0) and stats.surprise(1.0) == 0.0
    assert stats.surprise(0.0) == stats.SURPRISE_CAP


def test_cusum_grows_with_persistence_and_decays_when_quiet():
    assert stats.cusum([1] * 10, 0.1) == pytest.approx(9.0)
    assert stats.cusum([1] * 3 + [0] * 60, 0.1) == 0.0  # a short burst long ago leaves nothing
    assert stats.cusum([0, 1] * 10, 0.1) == pytest.approx(8.1)  # ten hits, nine quiet steps between them


# ---- features ------------------------------------------------------------------------------------------


def test_cards_and_payers_per_device_count_credentials_per_device():
    rows = [row(i, fp="bot", card=f"c{i}") for i in range(5)] + [
        row(10 + i, fp=f"shopper{i}", card=f"s{i}") for i in range(9)
    ]
    rows += [row(30, fp="wallet-bot", card=None, payer=f"p{i}", method="wallet") for i in range(3)]
    f = compute(rows, 15, NOW, BASE)
    assert f.cards_per_device_max == 5 and f.cards_per_device_p90 == 1.0
    assert f.payers_per_device_max == 3 and f.payers_per_device_p90 == 3.0  # one device used payers


def test_unapproved_wallet_share_counts_only_orders_old_enough_to_be_approved():
    rows = [row(200 + i, outcome="created", method="wallet", card=None) for i in range(4)]
    rows += [row(250, outcome="captured", method="wallet", card=None)]
    rows += [row(30, outcome="created", method="wallet", card=None)]  # 30 s old: may still be approved
    f = compute(rows, 15, NOW, BASE)
    assert f.aged_wallet_orders == 5 and f.unapproved_wallet_share == pytest.approx(0.8)


def test_interarrival_cv_tells_a_timer_from_shoppers():
    timer = compute([row(i * 10) for i in range(12)], 15, NOW, BASE)
    assert timer.interarrival_cv == pytest.approx(0.0)
    gaps = [1, 30, 2, 60, 5, 3, 90, 1, 15, 40]
    t, rows = 0.0, []
    for g in gaps:
        t += g
        rows.append(row(t))
    assert compute(rows, 15, NOW, BASE).interarrival_cv > 0.9
    assert compute([row(1), row(2), row(3)], 15, NOW, BASE).interarrival_cv == 1.0  # too few gaps: neutral


def test_error_spread_separates_an_outage_from_one_failing_device():
    outage = [row(i, fp=f"cust{i}", outcome="error", category="processor") for i in range(6)]
    f = compute(outage, 15, NOW, BASE)
    assert f.error_devices == 6 and f.error_top_device_share == pytest.approx(1 / 6)
    assert f.error_known_share == 1.0 and f.processor_share == 1.0
    assert f.error_top_sku_share == 1.0  # row() puts every attempt on the cheapest item
    spread = [row(i, fp=f"c{i}", sku=SKUS[i % 3], outcome="error", category="processor") for i in range(6)]
    assert compute(spread, 15, NOW, BASE).error_top_sku_share == pytest.approx(1 / 3)
    one = [row(i, fp="x", outcome="error", category="processor") for i in range(6)]
    g = compute(one, 15, NOW, BASE)
    assert g.error_devices == 1 and g.error_top_device_share == 1.0 and g.error_known_share == 0.0


def test_basket_diversity_and_amount_spread():
    same = compute([row(i) for i in range(6)], 15, NOW, BASE)
    assert same.distinct_skus == 1 and same.sku_entropy == 0.0 and same.amount_cv == 0.0
    mixed = compute([row(i, sku=SKUS[i % 3]) for i in range(6)], 15, NOW, BASE)
    assert mixed.distinct_skus == 3 and mixed.sku_entropy == pytest.approx(1.0) and mixed.amount_cv > 0.5


def test_decline_mix_shares_and_slow_decline_count():
    rows = [
        row(1, outcome="declined", category="fraud"),
        row(2, outcome="declined", category="probing"),
        row(3, outcome="declined", category="customer"),
        row(4, outcome="declined", category="unknown_decline"),
        row(5, outcome="error", category="processor"),
    ]
    f = compute(rows, 15, NOW, BASE)
    assert f.fraud_decline_share == 0.25 and f.probing_decline_share == 0.25
    assert f.customer_decline_share == 0.25 and f.processor_share == pytest.approx(0.2)
    assert f.non_customer_declines == 3


def test_velocity_test_expects_the_hour_of_day_rate_with_a_floor():
    quiet = Baseline(hourly_rate=tuple([0.1] * 24))
    busy = Baseline(hourly_rate=tuple(600.0 if h == NOW.hour else 0.1 for h in range(24)))
    rows = [row(i * 20) for i in range(10)]  # ten attempts in about three minutes
    assert compute(rows, 5, NOW, quiet).expected_attempts == pytest.approx(0.5)  # floored at 6 an hour
    assert compute(rows, 5, NOW, quiet).velocity_surprise > 8
    assert compute(rows, 5, NOW, busy).expected_attempts == pytest.approx(50.0)
    assert compute(rows, 5, NOW, busy).velocity_surprise < 0.01  # ten in five minutes is a slow evening
    assert "velocity_spike" in evaluate(compute_all(rows, NOW, quiet), quiet)
    assert "velocity_spike" not in evaluate(compute_all(rows, NOW, busy), busy)


def test_decline_spike_is_a_binomial_test_with_a_minimum_size_and_rate():
    def declines(n: int, bad: int) -> list[AttemptRow]:
        return [
            row(i * 60, fp=f"d{i}", sku=SKUS[i % 3], outcome="declined" if i < bad else "captured")
            for i in range(n)
        ]

    assert "decline_spike" in evaluate(compute_all(declines(10, 5), NOW, BASE), BASE)
    assert "decline_spike" not in evaluate(compute_all(declines(9, 9), NOW, BASE), BASE)  # too few
    assert "decline_spike" not in evaluate(compute_all(declines(14, 2), NOW, BASE), BASE)  # bad luck
    # A shop whose normal is 40% declines: 5 out of 10 is ordinary.
    high = Baseline(decline_rate=0.4)
    assert "decline_spike" not in evaluate(compute_all(declines(10, 5), NOW, high), high)


def slow_attack(n: int, every_s: float, start_s: float = 0, generic_only: bool = False) -> list[AttemptRow]:
    return [
        # Mostly unexplained refusals ("do not honour") and every third a wrong security code: no fraud code,
        # so fraud_declines stays quiet.
        row(
            start_s + i * every_s,
            fp=f"anon{i}",
            outcome="declined",
            category="probing" if i % 3 == 0 and not generic_only else "unknown_decline",
            domain=f"m{i}.top",
        )
        for i in range(n)
    ]


def test_slow_declines_catches_one_refusal_every_few_minutes():
    rows = slow_attack(18, 180)  # one every 3 minutes for the last 54 minutes, nothing else fires
    fired = evaluate(compute_all(rows, NOW, BASE), BASE)
    assert fired == ["slow_declines"]
    f60 = compute_all(rows, NOW, BASE)[60]
    assert f60.slow_decline_cusum >= 2.5 and f60.non_customer_declines == 18


def test_slow_declines_needs_recent_activity_and_ignores_honest_bad_luck():
    stopped = slow_attack(14, 180, start_s=16 * 60)  # nothing in the last 16 minutes
    assert "slow_declines" not in evaluate(compute_all(stopped, NOW, BASE), BASE)
    honest = [
        row(i * 180, fp=f"c{i}", outcome="declined", category="customer", sku=SKUS[i % 3]) for i in range(18)
    ]
    assert "slow_declines" not in evaluate(compute_all(honest, NOW, BASE), BASE)
    # Banks' generic refusals alone, however regular, are ordinary caution: no stolen-card or guessing code.
    generic = slow_attack(18, 180, generic_only=True)
    assert "slow_declines" not in evaluate(compute_all(generic, NOW, BASE), BASE)


def test_slow_declines_reference_tracks_a_busy_shop_but_stays_able_to_grow():
    """k is twice the shop's normal chance of a decline in a minute: a quiet shop is floored, a busy one is
    capped below 1, so a refusal-minute still adds to the sum (no blind spot at 300 attempts an hour)."""
    rows = slow_attack(30, 120)  # one every 2 minutes for the hour
    busy = Baseline(attempts_per_hour_mean=300, decline_rate=0.1, history_days=28)
    f60 = compute_all(rows, NOW, busy)[60]
    assert f60.slow_decline_cusum > 0
    assert features_mod.CUSUM_K_CAP < 1
    quiet = compute_all(rows, NOW, BASE)[60]
    assert quiet.slow_decline_cusum > f60.slow_decline_cusum  # a quiet shop is more sensitive


# ---- scorecard -----------------------------------------------------------------------------------------


def card_testing_rows(n: int = 30) -> list[AttemptRow]:
    cats = ["fraud", "probing", "unknown_decline", "customer"]
    return [
        row(
            i * 2,
            fp=f"bot{i % 3}",
            outcome="captured" if i % 4 == 0 else "declined",
            category=None if i % 4 == 0 else cats[i % 4],
            card=f"card{i}",
            domain=f"m{i}.top",
        )
        for i in range(n)
    ]


def rush_rows(n: int = 30) -> list[AttemptRow]:
    gaps = [3, 40, 7, 1, 25, 12, 60, 2, 9, 33]
    t, rows = 0.0, []
    for i in range(n):
        t += gaps[i % len(gaps)]
        rows.append(row(t, fp=f"cust{i % 20}", sku=SKUS[i % 3], card=f"k{i % 20}"))
    return rows


def outage_rows(n: int = 20) -> list[AttemptRow]:
    gaps = [3, 40, 7, 1, 25, 12, 60, 2, 9, 33]
    return [
        row(
            sum(gaps[: i % 10 + 1]) + i * 5,
            fp=f"cust{i}",
            sku=SKUS[i % 3],
            outcome="error" if i % 10 < 7 else "captured",
            category="processor" if i % 10 < 7 else None,
            card=f"k{i}",
        )
        for i in range(n)
    ]


def score(rows: list[AttemptRow], base: Baseline = BASE, sale: bool = False) -> classifier.ScoreResult:
    return classifier.classify(compute_all(rows, NOW, base), base, sale_active=sale)


def test_clear_card_testing_is_card_testing_with_validated_evidence():
    feats = compute_all(card_testing_rows(), NOW, BASE)
    r = classifier.classify(feats, BASE, sale_active=False)
    assert r.classification == "card_testing" and r.confidence >= 0.85 and not r.needs_tiebreak
    assert r.version == "scorecard-v1" and sum(r.probabilities.values()) == pytest.approx(1.0)
    ev = r.evidence()
    assert 3 <= len(ev) <= classifier.TOP_REASONS
    v = Verdict(
        classification="card_testing",
        confidence=r.confidence,
        summary="s",
        evidence=ev,
        recommended_actions=[],
    )
    kept, dropped = analyst.validate_evidence(v, feats)
    assert dropped == 0 and len(kept) == len(ev)  # valid by construction
    assert r.public()["top_reasons"] and r.public()["probabilities"]["card_testing"] == round(r.confidence, 3)


def test_a_rush_and_an_outage_are_told_apart_from_attacks():
    rush = score(rush_rows())
    assert rush.classification == "genuine_rush" and rush.confidence >= 0.7
    outage = score(outage_rows())
    assert outage.classification == "paypal_outage" and outage.confidence >= 0.7


def test_the_scorecard_is_deterministic():
    a, b = score(card_testing_rows()), score(card_testing_rows())
    assert a.public() == b.public()


def test_too_little_data_is_unclear():
    r = score(card_testing_rows(4))
    assert r.classification == "unclear" and r.needs_tiebreak
    assert "too little data to classify" in r.notes


def test_a_sale_window_is_never_the_sole_reason():
    # The announced sale adds at most as much as the rush's own traffic evidence, and can never turn an
    # attack into a rush (attackers strike during sales too).
    rows = card_testing_rows()
    without, with_sale = score(rows), score(rows, sale=True)
    own = sum(
        r.points for r in without.reasons if r.cls == "genuine_rush" and r.points > 0 and not r.supporting
    )
    assert with_sale.scores["genuine_rush"] - without.scores["genuine_rush"] <= own
    assert with_sale.classification == "card_testing"
    assert any(r.feature is None and r.supporting for r in with_sale.reasons)  # recorded as context
    assert all(e.feature for e in with_sale.evidence())  # never cited as evidence


def test_supporting_context_is_capped_by_traffic_evidence():
    features = compute_all(card_testing_rows(), NOW, BASE)
    r = classifier.classify(features, BASE, sale_active=True)
    mine = [x for x in r.reasons if x.cls == "genuine_rush"]
    evidence = sum(x.points for x in mine if x.points > 0 and not x.supporting)
    support = sum(x.points for x in mine if x.points > 0 and x.supporting)
    negative = sum(x.points for x in mine if x.points < 0)
    assert r.scores["genuine_rush"] == pytest.approx(evidence + min(support, evidence) + negative)


def test_cold_start_does_not_count_strangers_as_attack_evidence():
    cold = Baseline(decline_rate=0.03, history_days=2)
    r = score(card_testing_rows(), base=cold)
    assert not any(x.feature in ("new_email_domain_share", "known_customer_share") for x in r.reasons)
    assert any("cold start" in n for n in r.notes)
    assert r.classification == "card_testing"  # the refusals and the cards still say so
    warm = score(card_testing_rows())
    assert any(x.feature == "new_email_domain_share" for x in warm.reasons)


def test_an_attack_class_needs_a_card_account_or_refusal_signal():
    # A viral post about the cheapest item: strangers, one item, regular-ish arrivals, banks saying yes.
    rows = [row(i * 3, fp=f"viral{i}", card=f"v{i}", domain=f"n{i}.top") for i in range(20)]
    r = score(rows)
    assert r.scores["card_testing"] <= classifier.CORELESS_CAP
    assert r.classification not in ("card_testing", "account_testing")


def test_small_margin_or_low_top_probability_is_unclear():
    probs = classifier.softmax(
        {"card_testing": 2.0, "account_testing": 1.8, "genuine_rush": 0, "paypal_outage": 0}
    )
    assert probs["card_testing"] - probs["account_testing"] < classifier.MARGIN_UNCLEAR
    assert sum(probs.values()) == pytest.approx(1.0)


def test_account_testing_reads_unapproved_wallet_logins():
    rows = [
        row(
            130 + i * 3,
            fp=f"bot{i % 3}",
            method="wallet",
            card=None,
            payer=f"p{i}",
            outcome="created" if i % 5 else "declined",
            category=None if i % 5 else "fraud",
            domain=f"m{i}.top",
        )
        for i in range(30)
    ]
    r = score(rows)
    assert r.classification == "account_testing" and r.confidence >= 0.85
    assert any(x.feature == "unapproved_wallet_share" for x in r.top_reasons())


# ---- tie-break -----------------------------------------------------------------------------------------


def verdict(cls: str) -> Verdict:
    return Verdict(classification=cls, confidence=0.99, summary="s", evidence=[], recommended_actions=[])


def unsure(top: str = "card_testing", confidence: float = 0.6, margin: float = 0.1) -> classifier.ScoreResult:
    r = score(card_testing_rows())
    r.classification = "unclear" if margin < classifier.MARGIN_UNCLEAR else top
    r.top_class, r.confidence, r.margin = top, confidence, margin
    r.runner_up = "account_testing" if top != "account_testing" else "card_testing"
    return r


def test_a_confident_scorecard_ignores_gemini():
    sure = score(card_testing_rows())
    d = combine(sure, verdict("genuine_rush"))
    assert d.classification == "card_testing" and d.decided_by == DECIDED_BY_SCORECARD and d.tiebreak is None


def test_gemini_agreeing_confirms_the_top_class_capped_and_never_higher():
    d = combine(unsure(confidence=0.95), verdict("card_testing"), model_id="g")
    assert (
        d.classification == "card_testing" and d.confidence == AGREE_CAP and d.decided_by == DECIDED_BY_AGREE
    )
    low = combine(unsure(confidence=0.6), verdict("card_testing"))
    assert low.confidence == 0.6  # never above the scorecard's own


def test_gemini_disagreeing_or_naming_the_runner_up_leaves_it_unclear():
    for answer in ("genuine_rush", "account_testing", "unclear"):  # account_testing is the runner-up
        d = combine(unsure(), verdict(answer))
        assert d.classification == "unclear" and d.decided_by == DECIDED_BY_DISAGREE
        assert d.tiebreak["outcome"] == "disagree" and d.tiebreak["gemini"] == answer


def test_gemini_unavailable_or_unverified_leaves_the_scorecard_call():
    small_margin = unsure(margin=0.2)  # a class, but close enough to ask
    d = combine(small_margin, None)
    assert d.classification == "card_testing" and d.tiebreak["outcome"] == "unavailable"
    d = combine(small_margin, verdict("card_testing"), verified=False)
    assert d.classification == "card_testing" and d.decided_by == DECIDED_BY_SCORECARD
    assert d.tiebreak["outcome"] == "unverified"
    assert combine(unsure(), None).classification == "unclear"


# ---- explanation ---------------------------------------------------------------------------------------


@pytest.fixture
def explaining(monkeypatch):
    def use(answer: Verdict):
        async def fake(*, system, prompt, schema, temperature=0.1):
            assert system == analyst.EXPLAIN_SYSTEM and '"decision"' in prompt
            return answer.model_copy(deep=True), "gemini-x"

        monkeypatch.setattr("app.ai.analyst.generate_json", fake)

    return use


async def test_explain_keeps_the_decision_and_uses_the_words(explaining):
    feats = compute_all(card_testing_rows(), NOW, BASE)
    r = classifier.classify(feats, BASE, sale_active=False)
    ev = [
        Evidence(feature="decline_rate", window_min=15, observed=feats[15].decline_rate, why="Most refused.")
    ]
    explaining(
        Verdict(
            classification="card_testing",
            confidence=0.1,
            summary="Bots are trying stolen cards on your cheapest item.",
            evidence=ev,
            recommended_actions=["SCA_ON"],
        )
    )
    res = await analyst.explain({"windows": {}}, feats, r, "card_testing", r.confidence)
    assert res.ai_available and res.model_id == "gemini-x" and res.gemini_view == "card_testing"
    assert res.verdict.classification == "card_testing" and res.verdict.confidence == round(r.confidence, 3)
    assert res.verdict.summary == "Bots are trying stolen cards on your cheapest item."


async def test_explain_that_argues_for_another_class_falls_back_to_our_words(explaining):
    feats = compute_all(card_testing_rows(), NOW, BASE)
    r = classifier.classify(feats, BASE, sale_active=False)
    explaining(
        Verdict(
            classification="genuine_rush",
            confidence=0.9,
            summary="Just a busy evening.",
            evidence=[],
            recommended_actions=[],
        )
    )
    res = await analyst.explain({}, feats, r, "card_testing", r.confidence)
    assert res.gemini_view == "genuine_rush" and res.verdict.classification == "card_testing"
    assert res.verdict.summary.startswith(analyst.SUMMARY_LEAD["card_testing"])
    assert "read the traffic differently" in res.verdict.uncertainty_notes


def test_template_verdict_is_our_own_wording_with_scorecard_evidence():
    r = score(card_testing_rows())
    v = analyst.template_verdict("card_testing", r.confidence, r, ["a note."])
    assert v.summary.startswith(analyst.SUMMARY_LEAD["card_testing"]) and v.recommended_actions == []
    assert v.evidence and v.uncertainty_notes == "a note."
    unclear = analyst.template_verdict("unclear", 0.5, r)
    assert "leaned towards card testing" in unclear.summary


# ---- one shopper is never an attack; errors a client can cause are not an outage (review fixes) ----------


def viral_rush(n: int = 40, generic_every: int = 10) -> list[AttemptRow]:
    """First-time shoppers on the cheapest item; every `generic_every`-th refused with the bank's generic
    code (the commonest honest refusal)."""
    gaps = [3, 40, 7, 1, 25, 12, 60, 2, 9, 33]
    t, rows = 0.0, []
    for i in range(n):
        t += gaps[i % len(gaps)]
        generic = i % generic_every == 0
        rows.append(
            row(
                t,
                fp=f"viral{i}",
                card=f"v{i}:2030-12",
                outcome="declined" if generic else "captured",
                category="unknown_decline" if generic else None,
            )
        )
    return rows


def fumbler(at_s: float = 100) -> list[AttemptRow]:
    """One shopper: two wrong security codes on one card (two expiry guesses), a second card refused, a
    third card that goes through."""
    return [
        row(at_s, fp="fumbler", card="411111:2030-11", outcome="declined", category="probing"),
        row(at_s - 20, fp="fumbler", card="411111:2031-11", outcome="declined", category="probing"),
        row(at_s - 40, fp="fumbler", card="422222:2029-04", outcome="declined", category="unknown_decline"),
        row(at_s - 60, fp="fumbler", card="433333:2032-01"),
    ]


def test_cards_per_device_counts_card_numbers_not_expiry_guesses():
    f = compute(fumbler(), 15, NOW, BASE)
    assert f.cards_per_device_max == 3 and f.multi_card_devices == 1  # three numbers, not four keys
    assert f.suspect_declines == 2 and f.suspect_decline_devices == 1


@pytest.mark.parametrize("sale", [False, True])
def test_one_fumbling_shopper_never_turns_a_rush_into_an_attack(sale):
    rush = score(viral_rush(), sale=sale)
    both = score(viral_rush() + fumbler(), sale=sale)
    assert both.classification not in ("card_testing", "account_testing"), both.public()
    assert both.probabilities["card_testing"] < 0.5
    assert not any(r.cls == "card_testing" and classifier._is_core(r) for r in both.reasons)
    # The fumbler moves the card-testing score by at most the small non-core "more than one card" rule.
    assert both.scores["card_testing"] - rush.scores["card_testing"] <= 0.5 + 1e-9
    assert "slow_declines" not in evaluate(compute_all(viral_rush() + fumbler(), NOW, BASE), BASE)


def test_generic_refusals_alone_are_never_a_core_card_testing_signal():
    r = score(viral_rush(generic_every=4))  # 25% refused, all with the bank's generic code
    assert r.classification != "card_testing" and r.scores["card_testing"] <= classifier.CORELESS_CAP
    assert any("not with stolen-card" in x.text for x in r.reasons)


def test_card_testing_from_too_few_devices_is_capped_below_the_refund_floor():
    # One device cycling four cards with stolen-card refusals: an attack, but one device.
    rows = [
        row(i * 20, fp="lone", card=f"n{i % 4}", outcome="declined", category="fraud", domain=f"m{i}.top")
        for i in range(12)
    ]
    r = score(rows)
    assert r.top_class == "card_testing" and r.confidence <= classifier.SPREAD_CAP < 0.85
    assert any("too few devices" in n for n in r.notes)
    assert score(card_testing_rows()).confidence > classifier.SPREAD_CAP  # spread over devices: uncapped


def manufactured_errors(n: int = 40) -> list[AttemptRow]:
    """Capture calls on orders nobody approved, from a fresh device id each time."""
    return [
        AttemptRow(
            ts=NOW - timedelta(seconds=i * 15),
            sku=SKUS[i % 3],
            amount=10.0,
            method="card",
            fingerprint_hash=f"junk{i}",
            email_domain="x.top",
            card_key=None,
            payer_id_hash=None,
            outcome="error",
            decline_code="ORDER_NOT_APPROVED",
            decline_category="processor",
        )
        for i in range(n)
    ]


def test_errors_a_client_can_cause_are_not_outage_evidence():
    f = compute(manufactured_errors(), 15, NOW, BASE)
    assert f.client_errors == 40 and f.errors == 0 and f.error_rate == 0 and f.reached_paypal == 0
    rows = card_testing_rows() + manufactured_errors()
    r = score(rows)
    assert r.classification == "card_testing" and r.probabilities["paypal_outage"] < 0.05
    fired = evaluate(compute_all(rows, NOW, BASE), BASE)
    assert "processor_errors" not in fired and "decline_spike" in fired
    # A real outage (PayPal's 503s) is still one.
    assert score(outage_rows()).classification == "paypal_outage"


def test_an_ai_agreeing_with_too_little_data_stays_below_the_blocking_floor():
    r = score(card_testing_rows(4))
    assert r.too_little_data and r.needs_tiebreak
    r.confidence = 0.95  # however lopsided four rows look
    d = combine(r, verdict("card_testing"))
    assert d.decided_by == DECIDED_BY_AGREE and d.confidence <= LOW_DATA_CAP < 0.70


def test_unopposed_tiebreak_comes_from_the_recorded_decision_only():
    from types import SimpleNamespace

    def inc(decided_by, outcome=None):
        return SimpleNamespace(decided_by=decided_by, scorecard={"tiebreak": {"outcome": outcome}})

    assert unopposed_tiebreak(inc(DECIDED_BY_AGREE, "agree"))
    assert unopposed_tiebreak(inc(DECIDED_BY_SCORECARD, "unavailable"))
    assert unopposed_tiebreak(inc(DECIDED_BY_SCORECARD, "unverified"))
    assert not unopposed_tiebreak(inc(DECIDED_BY_DISAGREE, "disagree"))
    assert not unopposed_tiebreak(
        SimpleNamespace(decided_by=DECIDED_BY_SCORECARD, scorecard={"tiebreak": None})
    )
    assert not unopposed_tiebreak(SimpleNamespace(decided_by=None, scorecard=None))  # before the scorecard


async def test_an_off_topic_explanation_carries_no_advice_and_is_not_credited_to_the_model(explaining):
    feats = compute_all(rush_rows(), NOW, BASE)
    r = classifier.classify(feats, BASE, sale_active=False)
    explaining(
        Verdict(
            classification="card_testing",
            confidence=0.9,
            summary="Bots!",
            evidence=[],
            recommended_actions=["BLOCK_FP", "HOLD_SKU", "REFUND_MATCHED"],
            trend="escalating",
        )
    )
    res = await analyst.explain({}, feats, r, "genuine_rush", r.confidence)
    assert res.template_text and res.verdict.recommended_actions == [] and res.verdict.trend == "stable"
    # On topic, advice is still limited to what fits the decided class.
    explaining(
        Verdict(
            classification="genuine_rush",
            confidence=0.9,
            summary="A busy evening of real shoppers.",
            evidence=[],
            recommended_actions=["NOTIFY", "REFUND_MATCHED"],
        )
    )
    res = await analyst.explain({}, feats, r, "genuine_rush", r.confidence)
    assert not res.template_text and res.verdict.recommended_actions == ["NOTIFY"]


def test_decision_path_says_when_the_second_opinion_was_ignored():
    from types import SimpleNamespace

    from app.ai.reporter import decision_path

    def inc(outcome):
        return SimpleNamespace(
            classification="card_testing",
            classifier_version="scorecard-v1",
            decided_by=DECIDED_BY_SCORECARD,
            scorecard={"tiebreak": {"outcome": outcome}},
            model_id="template",
        )

    assert "did not match our numbers" in decision_path(inc("unverified"))
    assert "unavailable" not in decision_path(inc("unverified"))
    assert "was unavailable" in decision_path(inc("unavailable"))
