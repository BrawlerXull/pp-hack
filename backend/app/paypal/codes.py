"""One table for every PayPal decline/error code we store, and the rule that turns a PayPal answer
into an attempt outcome.

Why a table: PayPal reports a refusal in two shapes. A 201 capture with `status: DECLINED` carries a
numeric `processor_response.response_code` ("9500"); a 422 carries only an issue name
("INSTRUMENT_DECLINED"). Both are mapped to one canonical name and a category, so "stolen card" and
"honest shopper out of funds" look different to detection, and a decline never inflates the error
rate (which is the outage signal).

Default rule: codes in the table use their outcome. An unknown 422 issue on a card payment is a DECLINED
with category unknown_decline (422 is PayPal's "business validation failed", which for a card is the
issuer saying no). On a wallet it is an ERROR: wallet declines all carry a named issue, and an unknown
wallet 422 is far more likely our integration than the buyer. 0/5xx, unknown non-422 4xx (request
shape, auth) and the listed integration/concurrency problems are always ERROR, so a bad deploy or a
racing request shows up as the outage signal, never as a card-testing decline spike.
Numeric codes are from developer.paypal.com/api/rest/sandbox/card-testing/.
"""

from dataclasses import dataclass

from app.db.models import DeclineCategory, Outcome

C = DeclineCategory


@dataclass(frozen=True)
class Code:
    name: str
    category: DeclineCategory | None
    outcome: Outcome


def _declined(name: str, category: DeclineCategory) -> Code:
    return Code(name, category, Outcome.DECLINED)


# processor_response.response_code → canonical name
PROCESSOR_CODES: dict[str, Code] = {
    "0500": _declined("DO_NOT_HONOR", C.UNKNOWN_DECLINE),
    "5100": _declined("GENERIC_DECLINE", C.UNKNOWN_DECLINE),
    "5110": _declined("CVV2_FAILURE", C.PROBING),
    "00N7": _declined("CVV2_FAILURE_POSSIBLE_RETRY_WITH_CVV", C.PROBING),
    "5120": _declined("INSUFFICIENT_FUNDS", C.CUSTOMER),
    "5400": _declined("EXPIRED_CARD", C.CUSTOMER),
    "5180": _declined("INVALID_OR_RESTRICTED_CARD", C.FRAUD),
    "9500": _declined("SUSPECTED_FRAUD", C.FRAUD),
    "9520": _declined("LOST_OR_STOLEN", C.FRAUD),
    "1330": _declined("INVALID_ACCOUNT", C.FRAUD),
}

# Issue names (422 details[].issue, or our own names for capture/webhook declines).
_ISSUES: list[Code] = [
    _declined("INSTRUMENT_DECLINED", C.UNKNOWN_DECLINE),
    _declined("CARD_DECLINED", C.UNKNOWN_DECLINE),
    _declined("TRANSACTION_REFUSED", C.UNKNOWN_DECLINE),
    _declined("CAPTURE_DECLINED", C.UNKNOWN_DECLINE),
    _declined("DECLINED", C.UNKNOWN_DECLINE),
    _declined("DENIED", C.UNKNOWN_DECLINE),
    _declined("CARD_EXPIRED", C.CUSTOMER),
    _declined("PAYER_CANNOT_PAY", C.CUSTOMER),
    _declined("TRANSACTION_LIMIT_EXCEEDED", C.CUSTOMER),
    _declined("REDIRECT_PAYER_FOR_ALTERNATE_FUNDING", C.CUSTOMER),
    _declined("INVALID_SECURITY_CODE", C.PROBING),
    _declined("MAX_NUMBER_OF_PAYMENT_ATTEMPTS_EXCEEDED", C.PROBING),
    _declined("CARD_CLOSED", C.FRAUD),
    _declined("PAYER_ACCOUNT_RESTRICTED", C.FRAUD),
    _declined("PAYER_ACCOUNT_LOCKED_OR_CLOSED", C.FRAUD),
    _declined("TRANSACTION_BLOCKED_BY_PAYEE", C.FRAUD),
    _declined("COMPLIANCE_VIOLATION", C.FRAUD),
    # 3-D Secure step-up needed (a 422 on capture, or the order status on create).
    Code("PAYER_ACTION_REQUIRED", None, Outcome.SCA_REQUIRED),
    # Known integration / processor problems: our side or PayPal's, never the card.
    *(
        Code(name, C.PROCESSOR, Outcome.ERROR)
        for name in (
            "PAYEE_NOT_ENABLED_FOR_CARD_PROCESSING",
            "PAYEE_ACCOUNT_RESTRICTED",
            "PAYEE_ACCOUNT_LOCKED_OR_CLOSED",
            "ORDER_NOT_APPROVED",
            "ORDER_ALREADY_CAPTURED",
            "ORDER_EXPIRED",
            "ORDER_COMPLETION_IN_PROGRESS",
            "ORDER_COMPLETED_OR_VOIDED",
            "ORDER_CANNOT_BE_SAVED",
            "PREVIOUS_REQUEST_IN_PROGRESS",
            "DUPLICATE_REQUEST_ID",
            "AMOUNT_MISMATCH",
            "ITEM_TOTAL_MISMATCH",
            "CANNOT_BE_ZERO_OR_NEGATIVE",
            "CURRENCY_NOT_SUPPORTED",
            "PAYEE_ACCOUNT_INVALID",
            "PAYEE_BLOCKED_TRANSACTION",
            "DUPLICATE_INVOICE_ID",
            "INVALID_REQUEST",
            "INVALID_PARAMETER_VALUE",
            "MISSING_REQUIRED_PARAMETER",
            "MALFORMED_REQUEST_JSON",
            "AUTHENTICATION_FAILURE",
            "NOT_AUTHORIZED",
            "PERMISSION_DENIED",
            "RESOURCE_NOT_FOUND",
            "INVALID_RESOURCE_ID",
            "INTERNAL_SERVER_ERROR",
            "SERVICE_UNAVAILABLE",
            "NETWORK_ERROR",
            "AUTH_ERROR",
        )
    ),
]
BY_NAME: dict[str, Code] = {c.name: c for c in [*PROCESSOR_CODES.values(), *_ISSUES]}

# Name-on-card triggers that make the sandbox decline a card with that processor code.
REJECTION_TRIGGERS: dict[str, str] = {
    "CCREJECT-REFUSED": "0500",
    "CCREJECT-SF": "9500",
    "CCREJECT-EC": "5400",
    "CCREJECT-IRC": "5180",
    "CCREJECT-IF": "5120",
    "CCREJECT-LS": "9520",
    "CCREJECT-IA": "1330",
    "CCREJECT-BANK_ERROR": "5100",
    "CCREJECT-CVV_F": "00N7",
}


def lookup(code: str | None) -> Code | None:
    """A numeric processor code or an issue/canonical name."""
    if not code:
        return None
    return PROCESSOR_CODES.get(code) or BY_NAME.get(code.upper())


def trigger_code(card_name: str | None) -> str | None:
    """Processor code a CCREJECT-* trigger in the card name asks the sandbox for (longest trigger first,
    so a short trigger never shadows a longer one)."""
    if not card_name:
        return None
    for trigger in sorted(REJECTION_TRIGGERS, key=len, reverse=True):
        if trigger in card_name:
            return REJECTION_TRIGGERS[trigger]
    return None


@dataclass(frozen=True)
class Classification:
    outcome: Outcome
    decline_code: str | None = None
    category: DeclineCategory | None = None
    processor_code: str | None = None


def _from_code(code: Code, processor_code: str | None) -> Classification:
    if code.outcome == Outcome.SCA_REQUIRED:
        return Classification(Outcome.SCA_REQUIRED)
    return Classification(code.outcome, code.name, code.category, processor_code)


def classify(
    *,
    http: int,
    order_status: str | None,
    capture_status: str | None,
    issue: str | None,
    processor_code: str | None = None,
    card: bool = True,
) -> Classification:
    """`card`: the attempt pays by card. Only then is an unknown 422 read as an issuer decline."""
    if http == 0 or http >= 500:
        return Classification(Outcome.ERROR, issue or f"HTTP_{http}", C.PROCESSOR)
    if capture_status in {"DECLINED", "FAILED"}:
        # Checked before order status: a declined capture can come back inside a COMPLETED order.
        code = lookup(processor_code) or lookup(issue)
        if code and code.outcome == Outcome.DECLINED:
            return Classification(Outcome.DECLINED, code.name, code.category, processor_code)
        return Classification(
            Outcome.DECLINED, processor_code or issue or "CAPTURE_DECLINED", C.UNKNOWN_DECLINE, processor_code
        )
    if capture_status == "COMPLETED" or order_status == "COMPLETED":
        return Classification(Outcome.CAPTURED)
    if order_status == "PAYER_ACTION_REQUIRED":
        return Classification(Outcome.SCA_REQUIRED)
    if http >= 400:
        code = lookup(issue)
        if code:
            return _from_code(code, processor_code)
        if http == 422 and card:
            return Classification(
                Outcome.DECLINED, issue or "UNPROCESSABLE_ENTITY", C.UNKNOWN_DECLINE, processor_code
            )
        return Classification(Outcome.ERROR, issue or f"HTTP_{http}", C.PROCESSOR, processor_code)
    if order_status in {"CREATED", "APPROVED", "SAVED"}:
        return Classification(Outcome.CREATED)
    return Classification(Outcome.ERROR, issue or f"UNEXPECTED_STATUS_{order_status}", C.PROCESSOR)


def decline_for_webhook(processor_code: str | None, fallback: str) -> Classification:
    """PAYMENT.CAPTURE.DECLINED/DENIED: the resource's processor_response decides the name."""
    return classify(
        http=200, order_status=None, capture_status="DECLINED", issue=fallback, processor_code=processor_code
    )
