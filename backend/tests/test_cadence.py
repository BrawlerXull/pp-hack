"""When an incident is re-assessed and Gemini asked again (app/incidents/cadence.py). Pure: transient
Incident objects, no database."""

from datetime import UTC, datetime, timedelta

from app.db.models import Incident
from app.incidents import cadence
from app.incidents.decision import DECIDED_BY_AGREE, DECIDED_BY_SCORECARD

T0 = datetime(2026, 10, 18, 2, 0, tzinfo=UTC)
BASE = timedelta(minutes=2)


def inc(**kw) -> Incident:
    fields = dict(
        classification="card_testing",
        confidence=0.95,
        model_id="gemini-x",
        triggers=["decline_spike", "velocity_spike"],
        last_assessed_at=T0,
        verdict_at=T0,
        ai_refresh_failures=0,
        ai_refresh_failed_at=None,
        assessment_context=cadence.context(
            triggers=["decline_spike", "velocity_spike"],
            sale_window=False,
            attempts_15m=40,
            verdict_triggers=["decline_spike", "velocity_spike"],
        ),
    )
    return Incident(**(fields | kw))


def reason(i: Incident, after: timedelta, **kw) -> str | None:
    args = dict(now=T0 + after, fired=True, sale_active=False, attempts_15m=40, new_captures=False, base=BASE)
    return cadence.reassess_reason(i, **(args | kw))


def test_nothing_material_means_no_new_ai_call():
    assert reason(inc(), timedelta(minutes=3)) is None
    assert reason(inc(), timedelta(minutes=9), new_captures=True) is None  # confident enough to refund


def test_never_assessed_is_asked_at_once():
    assert reason(inc(last_assessed_at=None), timedelta(0)) == "never_assessed"


def test_new_trigger_type_and_sale_window_changes():
    i = inc(triggers=["decline_spike", "velocity_spike", "sku_concentration"])
    assert reason(i, timedelta(seconds=10)) is None  # minimum gap even for a material change
    assert reason(i, timedelta(seconds=40)) == "new_trigger"
    assert reason(inc(), timedelta(seconds=40), sale_active=True) == "sale_window_changed"


def test_rules_fallback_is_retried_with_backoff():
    for failures, wait in ((1, 2), (2, 4), (3, 8), (6, 8)):
        i = inc(model_id=cadence.RULES_FALLBACK, classification="unclear", ai_refresh_failures=failures)
        assert reason(i, timedelta(minutes=wait) - timedelta(seconds=1)) is None
        assert reason(i, timedelta(minutes=wait)) == "retry_after_ai_failure"
    # A failed re-check that kept a real verdict waits on the same backoff instead of every tick.
    kept = inc(ai_refresh_failures=1, ai_refresh_failed_at=T0)
    assert reason(kept, timedelta(minutes=1)) is None
    assert reason(kept, timedelta(minutes=2)) == "retry_after_ai_failure"


def test_low_confidence_reasks_on_growth_or_new_captures():
    unsure = inc(confidence=0.6)
    assert reason(unsure, timedelta(minutes=3), attempts_15m=59) is None
    assert reason(unsure, timedelta(minutes=3), attempts_15m=60) == "attempts_grew"
    no_refund = inc(confidence=0.8)
    assert reason(no_refund, timedelta(minutes=3), attempts_15m=100) is None  # acts, so growth alone is fine
    assert reason(no_refund, timedelta(minutes=3), new_captures=True) == "new_captures"
    assert reason(no_refund, timedelta(minutes=1), new_captures=True) is None  # not before the base interval


def test_a_real_sale_is_not_reasked_on_growth_or_captures():
    # A confident harmless verdict means no friction (R2), so a rush that keeps selling costs no extra calls.
    for cls in ("genuine_rush", "paypal_outage"):
        rush = inc(classification=cls, confidence=0.7)
        assert reason(rush, timedelta(minutes=3), attempts_15m=100, new_captures=True) is None
        assert reason(rush, timedelta(minutes=10)) == "verdict_aged"


def test_a_weak_harmless_verdict_is_reasked_like_unclear():
    # Below the floor R2 falls through to R3 (3-D Secure on): growth must get a better verdict, which
    # may RELAX it, instead of keeping the friction until the verdict ages out.
    for cls in ("genuine_rush", "paypal_outage"):
        weak = inc(classification=cls, confidence=0.55)
        assert reason(weak, timedelta(minutes=3), attempts_15m=60) == "attempts_grew"
        assert reason(weak, timedelta(minutes=3), new_captures=True) == "new_captures"


def test_old_verdict_is_reasked_only_while_signals_fire():
    assert reason(inc(), timedelta(minutes=10)) == "verdict_aged"
    assert reason(inc(), timedelta(minutes=10), fired=False) is None


def test_guard1_contradicting_triggers_break_a_kept_verdict():
    rush = inc(classification="genuine_rush", triggers=["velocity_spike", "decline_spike"])
    rush.assessment_context = cadence.context(
        triggers=["velocity_spike"], sale_window=True, attempts_15m=40, verdict_triggers=["velocity_spike"]
    )
    assert cadence.contradicting_triggers(rush) == {"decline_spike"}
    assert (
        cadence.contradicting_triggers(inc()) == set()
    )  # an attack verdict is not weakened by fraud signals
    # ... but it is by PayPal starting to fail after it was made (shoppers retrying look like bots).
    outage = inc(triggers=["decline_spike", "velocity_spike", "processor_errors"])
    assert cadence.contradicting_triggers(outage) == {"processor_errors"}
    assert cadence.contradicting_triggers(inc(classification="unclear", triggers=outage.triggers)) == set()
    seen = inc(classification="paypal_outage", triggers=["processor_errors", "sku_concentration"])
    seen.assessment_context = cadence.context(
        triggers=[], sale_window=False, attempts_15m=0, verdict_triggers=["sku_concentration"]
    )
    assert cadence.contradicting_triggers(seen) == set()


def test_guard2_staleness_after_three_failures_or_twenty_minutes():
    now = T0 + timedelta(minutes=5)
    assert not cadence.verdict_stale(inc(), now)
    assert not cadence.verdict_stale(inc(ai_refresh_failures=2, ai_refresh_failed_at=now), now)
    assert cadence.verdict_stale(inc(ai_refresh_failures=3, ai_refresh_failed_at=now), now)
    assert cadence.verdict_stale(
        inc(ai_refresh_failures=1, ai_refresh_failed_at=now), T0 + timedelta(minutes=20)
    )


def test_has_real_verdict_means_an_agreed_tie_break():
    """P9: the scorecard always answers, so the only AI-dependent verdict (the one kept through a failed
    re-check) is a tie-break Gemini agreed to. Used to mean "any non-fallback AI verdict"."""
    assert not cadence.has_real_verdict(Incident(classification=None, model_id=None))
    assert not cadence.has_real_verdict(inc(decided_by=DECIDED_BY_SCORECARD))
    assert cadence.has_real_verdict(inc(decided_by=DECIDED_BY_AGREE))


def test_gemini_is_retried_on_a_backoff_but_the_scorecard_never_waits():
    assert cadence.ai_due(inc(), T0, BASE)
    failing = inc(ai_refresh_failures=2)
    failing.assessment_context = failing.assessment_context | {"ai_at": T0.isoformat()}
    assert not cadence.ai_due(failing, T0 + timedelta(minutes=3), BASE)  # 2 failures: 4 min backoff
    assert cadence.ai_due(failing, T0 + timedelta(minutes=4), BASE)
    # A material change still re-assesses during the backoff (the scorecard re-runs; Gemini is skipped).
    assert reason(failing, timedelta(minutes=1), attempts_15m=100) is None  # confident: growth alone is fine
    unsure = inc(confidence=0.6, ai_refresh_failures=2)
    assert reason(unsure, timedelta(minutes=3), attempts_15m=60) == "attempts_grew"


def test_r0_only_acts_on_triggers_a_confident_harmless_verdict_has_not_weighed():
    fired = ["decline_spike", "velocity_spike", "fraud_declines"]
    rush = inc(classification="genuine_rush", confidence=0.9)  # its verdict saw decline_spike + velocity
    assert cadence.unjudged_triggers(rush, fired) == ["fraud_declines"]
    # A weak rush verdict or an attack verdict: R0 sees everything.
    assert cadence.unjudged_triggers(inc(classification="genuine_rush", confidence=0.6), fired) == fired
    assert cadence.unjudged_triggers(inc(), fired) == fired
    # P9: a confident scorecard rush counts whoever wrote its text (it used to need an AI verdict).
    template_rush = inc(model_id=cadence.TEMPLATE, classification="genuine_rush")
    assert cadence.unjudged_triggers(template_rush, fired) == ["fraud_declines"]


def test_sku_concentration_is_not_a_fraud_signal_for_guard1():
    # A single-product sale trips sku_concentration; it must not break a kept genuine_rush verdict.
    rush = inc(classification="genuine_rush", triggers=["velocity_spike", "sku_concentration"])
    rush.assessment_context = cadence.context(
        triggers=["velocity_spike"], sale_window=False, attempts_15m=40, verdict_triggers=["velocity_spike"]
    )
    assert cadence.contradicting_triggers(rush) == set()
    rush.triggers = [*rush.triggers, "cheap_sku_card_cycling"]
    assert cadence.contradicting_triggers(rush) == {"cheap_sku_card_cycling"}
