"""Analyst retry / confidence cap and the reporter's AI path (digits guard, skip when Gemini is down)."""

from datetime import UTC, datetime, timedelta

import pytest

from app.ai import analyst, breaker, gemini, reporter
from app.ai.schemas import Evidence, ReportDraft, Verdict
from app.config import Settings
from app.db.models import CooldownReason, Incident
from app.detection.features import Features
from tests.conftest import SHOP

FEATURES = {15: Features(window_min=15, attempts=60, decline_rate=0.8)}


def verdict(classification: str, observed: float | None, confidence: float = 0.95) -> Verdict:
    ev = [] if observed is None else [Evidence(feature="attempts", window_min=15, observed=observed, why="x")]
    return Verdict(
        classification=classification,
        confidence=confidence,
        summary="s",
        evidence=ev,
        recommended_actions=["SCA_ON"],
    )


def scripted(monkeypatch, module: str, answers: list):
    prompts: list[str] = []

    async def fake(*, system, prompt, schema, temperature=0.1):
        prompts.append(prompt)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer, "mock-model"

    monkeypatch.setattr(f"app.ai.{module}.generate_json", fake)
    return prompts


# ---- analyst ---------------------------------------------------------------------------------------


async def test_unverified_evidence_is_retried_once_then_accepted(monkeypatch):
    prompts = scripted(monkeypatch, "analyst", [verdict("card_testing", 999), verdict("card_testing", 60)])
    res = await analyst.analyse({"x": 1}, FEATURES)
    assert len(prompts) == 2 and "did not match" in prompts[1]
    assert res.ai_available and res.verdict.confidence == 0.95 and len(res.verdict.evidence) == 1


async def test_evidence_still_unverified_after_retry_caps_confidence(monkeypatch):
    scripted(monkeypatch, "analyst", [verdict("card_testing", 999), verdict("card_testing", 500)])
    res = await analyst.analyse({"x": 1}, FEATURES)
    v = res.verdict
    assert v.classification == "card_testing" and v.confidence == 0.6 and v.evidence == []
    assert "could not be verified" in v.uncertainty_notes and res.dropped_evidence == 1


async def test_unclear_verdict_without_evidence_is_not_retried(monkeypatch):
    prompts = scripted(monkeypatch, "analyst", [verdict("unclear", None, confidence=0.4)])
    res = await analyst.analyse({"x": 1}, FEATURES)
    assert len(prompts) == 1 and res.verdict.confidence == 0.4


async def test_ai_unavailable_uses_the_breaker_reason(monkeypatch):
    scripted(
        monkeypatch, "analyst", [gemini.AIUnavailable("all cooling down", "Gemini daily quota exhausted")]
    )
    res = await analyst.analyse({"x": 1}, FEATURES)
    assert not res.ai_available and res.model_id == "rules-fallback"
    assert "Gemini daily quota exhausted" in res.verdict.uncertainty_notes


# ---- reporter --------------------------------------------------------------------------------------


@pytest.fixture
async def incident(db):
    now = datetime.now(UTC)
    inc = Incident(
        shop_id=SHOP,
        opened_at=now - timedelta(minutes=5),
        classification="card_testing",
        confidence=0.95,
        verdict={"summary": "A bot is testing stolen cards."},
    )
    db.add(inc)
    await db.flush()
    return inc


async def test_reporter_keeps_ai_prose_but_rejects_drafts_with_digits(db, incident, monkeypatch):
    draft = ReportDraft(
        alert="I stopped 5 test payments.",  # a number the model made up: falls back to the template
        report_md="# Report\n\nA {{classification}} hit {{targeted_item}}; {{refunded_count}} refunded.",
        evidence_md="1. Merchant {{shop_name}} saw a {{classification}}.",  # list markers are fine
    )
    prompts = scripted(monkeypatch, "reporter", [draft])
    texts, model_id = await reporter.write(db, incident)
    assert len(prompts) == 1 and model_id == "mock-model"
    assert "5 test payments" not in texts["alert_text"] and "card-testing attack" in texts["alert_text"]
    assert texts["report_md"].startswith("# Report") and "{{" not in texts["report_md"]
    assert texts["evidence_md"] == "1. Merchant Wick & Wax saw a card-testing attack."


async def test_reporter_skips_gemini_when_the_analyst_just_failed(db, incident, monkeypatch):
    prompts = scripted(monkeypatch, "reporter", [])
    texts, model_id = await reporter.write(db, incident, skip_ai=True)
    assert prompts == [] and model_id == "template" and "card-testing attack" in texts["alert_text"]


async def test_reporter_skips_gemini_while_every_model_cools_down(db, incident, monkeypatch):
    settings = Settings(_env_file=None, gemini_api_key="k", gemini_model="m1", gemini_fallback_models="")
    monkeypatch.setattr(gemini, "get_settings", lambda: settings)
    gemini._breaker.trip("m1", breaker.Trip(CooldownReason.QUOTA, 60), datetime.now(UTC))
    assert await gemini.cooling_down()
    prompts = scripted(monkeypatch, "reporter", [])
    _, model_id = await reporter.write(db, incident)
    assert prompts == [] and model_id == "template"


async def test_reporter_calls_gemini_again_once_the_shared_pause_is_gone(db, incident, monkeypatch):
    """A stale in-memory "key rejected" (until restart) must not keep owner close/regenerate on the template
    after the worker's restart cleared the shared row."""
    settings = Settings(_env_file=None, gemini_api_key="k", gemini_model="m1", gemini_fallback_models="")
    monkeypatch.setattr(gemini, "get_settings", lambda: settings)
    monkeypatch.setattr(gemini, "store", breaker.DbStore())
    gemini._breaker.trip("m1", breaker.Trip(CooldownReason.BAD_KEY, None, all_models=True), datetime.now(UTC))
    draft = ReportDraft(
        alert="A {{classification}} was stopped.", report_md="# Report", evidence_md="Summary."
    )
    prompts = scripted(monkeypatch, "reporter", [draft])
    _, model_id = await reporter.write(db, incident)
    assert len(prompts) == 1 and model_id == "mock-model"
