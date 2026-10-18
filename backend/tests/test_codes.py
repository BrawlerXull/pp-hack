"""Decline-code table, outcome classification, offline decline derivation and live card rotation (pure)."""

from decimal import Decimal

import pytest

from app.db.models import DeclineCategory, Method, Outcome
from app.detection.features import card_key
from app.paypal import codes
from app.paypal.orders import CardDetails, order_payload
from app.shop.service import OfflinePlan, classify, offline_result
from app.simulator.scenarios import (
    DECLINE_TRIGGERS,
    SANDBOX_TEST_CARDS,
    card_testing,
    customer_card,
    flash_sale,
    slow_drip,
)
from tests.sandbox_double import SANDBOX_REJECTION_CODES

C = DeclineCategory


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        # 201 + capture DECLINED inside a COMPLETED order: a decline, named from the processor code.
        (
            {
                "http": 201,
                "order_status": "COMPLETED",
                "capture_status": "DECLINED",
                "processor_code": "9500",
            },
            (Outcome.DECLINED, "SUSPECTED_FRAUD", C.FRAUD, "9500"),
        ),
        (
            {
                "http": 201,
                "order_status": "COMPLETED",
                "capture_status": "DECLINED",
                "processor_code": "5120",
            },
            (Outcome.DECLINED, "INSUFFICIENT_FUNDS", C.CUSTOMER, "5120"),
        ),
        (
            {
                "http": 201,
                "order_status": "COMPLETED",
                "capture_status": "DECLINED",
                "processor_code": "00N7",
            },
            (Outcome.DECLINED, "CVV2_FAILURE_POSSIBLE_RETRY_WITH_CVV", C.PROBING, "00N7"),
        ),
        (
            {
                "http": 201,
                "order_status": "COMPLETED",
                "capture_status": "DECLINED",
                "processor_code": "7777",
            },
            (Outcome.DECLINED, "7777", C.UNKNOWN_DECLINE, "7777"),
        ),
        # 422 with an issue name only.
        (
            {"http": 422, "issue": "INSTRUMENT_DECLINED"},
            (Outcome.DECLINED, "INSTRUMENT_DECLINED", C.UNKNOWN_DECLINE, None),
        ),
        (
            {"http": 422, "issue": "PAYER_ACCOUNT_RESTRICTED"},
            (Outcome.DECLINED, "PAYER_ACCOUNT_RESTRICTED", C.FRAUD, None),
        ),
        (
            {"http": 422, "issue": "INSUFFICIENT_FUNDS"},
            (Outcome.DECLINED, "INSUFFICIENT_FUNDS", C.CUSTOMER, None),
        ),
        (
            {"http": 422, "issue": "SOME_NEW_ISSUE"},
            (Outcome.DECLINED, "SOME_NEW_ISSUE", C.UNKNOWN_DECLINE, None),
        ),
        ({"http": 422, "issue": "PAYER_ACTION_REQUIRED"}, (Outcome.SCA_REQUIRED, None, None, None)),
        # Integration / processor problems stay errors (the outage signal).
        (
            {"http": 422, "issue": "PAYEE_NOT_ENABLED_FOR_CARD_PROCESSING"},
            (Outcome.ERROR, "PAYEE_NOT_ENABLED_FOR_CARD_PROCESSING", C.PROCESSOR, None),
        ),
        (
            {"http": 400, "issue": "INVALID_PARAMETER_SYNTAX"},
            (Outcome.ERROR, "INVALID_PARAMETER_SYNTAX", C.PROCESSOR, None),
        ),
        # Integration / concurrency 422s: our side, never the card (would fake a decline spike).
        *(
            ({"http": 422, "issue": issue}, (Outcome.ERROR, issue, C.PROCESSOR, None))
            for issue in (
                "AMOUNT_MISMATCH",
                "ITEM_TOTAL_MISMATCH",
                "CURRENCY_NOT_SUPPORTED",
                "PREVIOUS_REQUEST_IN_PROGRESS",
                "ORDER_COMPLETION_IN_PROGRESS",
                "DUPLICATE_REQUEST_ID",
                "PAYEE_BLOCKED_TRANSACTION",
            )
        ),
        # An unknown 422 is a decline only for a card; on a wallet it is an integration error.
        (
            {"http": 422, "issue": "SOME_NEW_ISSUE", "card": False},
            (Outcome.ERROR, "SOME_NEW_ISSUE", C.PROCESSOR, None),
        ),
        (
            {"http": 422, "issue": "INSTRUMENT_DECLINED", "card": False},
            (Outcome.DECLINED, "INSTRUMENT_DECLINED", C.UNKNOWN_DECLINE, None),
        ),
        ({"http": 503, "issue": None}, (Outcome.ERROR, "HTTP_503", C.PROCESSOR, None)),
        ({"http": 0, "issue": "AUTH_ERROR"}, (Outcome.ERROR, "AUTH_ERROR", C.PROCESSOR, None)),
        # Non-declines.
        ({"http": 200, "order_status": "PAYER_ACTION_REQUIRED"}, (Outcome.SCA_REQUIRED, None, None, None)),
        (
            {"http": 201, "order_status": "COMPLETED", "capture_status": "COMPLETED"},
            (Outcome.CAPTURED, None, None, None),
        ),
        ({"http": 201, "order_status": "CREATED"}, (Outcome.CREATED, None, None, None)),
    ],
)
def test_classify(kw, expected):
    args = {"order_status": None, "capture_status": None, "issue": None, "processor_code": None} | kw
    c = codes.classify(**args)
    assert (c.outcome, c.decline_code, c.category, c.processor_code) == expected


def test_every_trigger_maps_to_a_named_decline():
    for trigger, code in codes.REJECTION_TRIGGERS.items():
        info = codes.lookup(code)
        assert info is not None and info.outcome == Outcome.DECLINED, trigger
    assert codes.trigger_code("CCREJECT-IRC") == "5180"
    assert codes.trigger_code("Test Buyer") is None
    assert codes.lookup("insufficient_funds") == codes.lookup("5120")
    # The sandbox double keeps its own copy of PayPal's table: the two must agree.
    assert SANDBOX_REJECTION_CODES == codes.REJECTION_TRIGGERS


def test_offline_card_decline_is_derived_from_the_trigger():
    plan = OfflinePlan("decline", decline_code="IGNORED_FOR_CARDS")
    res = offline_result(plan, False, Method.CARD, CardDetails(number="4111111111111111", name="CCREJECT-SF"))
    c = classify(res)
    assert (res.http, res.capture_status, res.processor_code) == (201, "DECLINED", "9500")
    assert res.capture_id is None  # a declined SIM row never shows a capture id
    assert (c.outcome, c.decline_code, c.category) == (Outcome.DECLINED, "SUSPECTED_FRAUD", C.FRAUD)

    wallet = classify(offline_result(OfflinePlan("decline", "PAYER_CANNOT_PAY"), False, Method.WALLET, None))
    assert (wallet.outcome, wallet.category) == (Outcome.DECLINED, C.CUSTOMER)


def test_offline_card_testing_codes_match_their_triggers():
    """SimAttempt.decline_code (used by eval_ai) agrees with what the shop flow stores."""
    for a in card_testing("offline", count=30, seed=3):
        if a.outcome != "decline":
            continue
        assert a.card_name in DECLINE_TRIGGERS
        c = classify(offline_result(OfflinePlan(a.outcome, a.decline_code), False, a.method, _card(a)))
        assert c.outcome == Outcome.DECLINED and c.decline_code == a.decline_code


def test_sca_order_payload_has_return_urls():
    card = CardDetails(number="4012888888881881", name="Test Buyer")
    base = {"sku": "S", "description": "d", "amount": Decimal("3.10"), "custom_id": "shop:a1", "card": card}
    sca = order_payload(**base, force_sca=True, return_url="https://x/r", cancel_url="https://x/c")
    src = sca["payment_source"]["card"]
    assert src["attributes"] == {"verification": {"method": "SCA_ALWAYS"}}
    assert src["experience_context"] == {"return_url": "https://x/r", "cancel_url": "https://x/c"}
    plain = order_payload(**base, return_url="https://x/r", cancel_url="https://x/c")
    assert "experience_context" not in plain["payment_source"]["card"]


def _card(a) -> CardDetails | None:
    return (
        CardDetails(number=a.card_number, name=a.card_name, expiry=a.card_expiry) if a.card_number else None
    )


def _unique_cards_ratio(attempts) -> float:
    keys = [card_key(a.card_number[:6], a.card_number[-4:], a.card_expiry) for a in attempts if a.card_number]
    return len(set(keys)) / len(keys)


@pytest.mark.parametrize("gen", [card_testing, slow_drip])
def test_live_attacks_rotate_sandbox_cards(gen):
    attempts = list(gen("live", count=120, seed=5))
    assert {a.card_number for a in attempts} <= set(SANDBOX_TEST_CARDS)
    assert len({a.card_number for a in attempts}) > 1
    assert _unique_cards_ratio(attempts) > 0.5


def test_live_customers_keep_their_card():
    assert customer_card("cust-001") == customer_card("cust-001")
    rush = [a for a in flash_sale("live", count=60, seed=2) if a.device_id.startswith("cust-")]
    by_device: dict[str, set[tuple[str | None, str]]] = {}
    for a in rush:
        by_device.setdefault(a.device_id, set()).add((a.card_number, a.card_expiry))
    assert all(len(cards) == 1 for cards in by_device.values())


def test_card_key_includes_expiry():
    assert card_key("401288", "1881", "2030-12") != card_key("401288", "1881", "2031-01")
    assert card_key("401288", "1881", None) == "4012881881"
    assert card_key(None, None, "2030-12") is None
