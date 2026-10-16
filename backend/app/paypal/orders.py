"""Orders v2 + Payments helpers. Pure payload builders + small result parsers, so they're easy to test."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.paypal.client import PayPalClient, PayPalResponse

# Order create/capture issues that mean "the card or account was declined" (vs. a processor error).
DECLINE_ISSUES = {
    "INSTRUMENT_DECLINED",
    "CARD_DECLINED",
    "TRANSACTION_REFUSED",
    "PAYER_ACTION_REQUIRED",
    "CARD_EXPIRED",
    "INVALID_SECURITY_CODE",
    "CARD_CLOSED",
    "PAYER_CANNOT_PAY",
    "TRANSACTION_BLOCKED_BY_PAYEE",
    "COMPLIANCE_VIOLATION",
}


@dataclass
class CardDetails:
    """Sandbox test card. Only the simulator ever sends raw card numbers (sandbox-only)."""

    number: str
    name: str
    expiry: str = "2030-12"
    security_code: str = "123"


def order_payload(
    *,
    sku: str,
    description: str,
    amount: Decimal,
    custom_id: str,
    card: CardDetails | None = None,
    force_sca: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "reference_id": sku,
                "description": description,
                "custom_id": custom_id,
                "amount": {"currency_code": "USD", "value": f"{amount:.2f}"},
            }
        ],
    }
    card_source: dict[str, Any] | None = None
    if card:
        card_source = {
            "number": card.number,
            "expiry": card.expiry,
            "security_code": card.security_code,
            "name": card.name,
        }
    if force_sca:
        # Per-order 3-D Secure: SCA_ALWAYS asks the issuer to authenticate every eligible card.
        card_source = card_source or {}
        card_source["attributes"] = {"verification": {"method": "SCA_ALWAYS"}}
    if card_source is not None:
        payload["payment_source"] = {"card": card_source}
    return payload


@dataclass
class OrderResult:
    http: int
    order_id: str | None
    status: str | None
    capture_id: str | None
    capture_status: str | None
    issue: str | None
    debug_id: str | None
    latency_ms: int
    payer_action_url: str | None = None

    @property
    def is_processor_error(self) -> bool:
        return self.http >= 500 or self.http == 0


def parse_order(res: PayPalResponse) -> OrderResult:
    body = res.json if isinstance(res.json, dict) else {}
    units = body.get("purchase_units") or [{}]
    captures = (units[0].get("payments") or {}).get("captures") or []
    cap = captures[0] if captures else {}
    err = res.error_summary() or {}
    issue = next((i for i in err.get("issues", []) if i), None) or err.get("name")
    if cap.get("status") == "DECLINED":
        issue = issue or (cap.get("processor_response") or {}).get("response_code") or "CAPTURE_DECLINED"
    return OrderResult(
        http=res.status,
        order_id=body.get("id"),
        status=body.get("status"),
        capture_id=cap.get("id"),
        capture_status=cap.get("status"),
        issue=issue,
        debug_id=res.debug_id,
        latency_ms=res.elapsed_ms,
        payer_action_url=next(
            (link["href"] for link in body.get("links", []) if link.get("rel") == "payer-action"), None
        ),
    )


async def create_order(client: PayPalClient, payload: dict[str, Any]) -> OrderResult:
    res = await client.request(
        "POST", "/v2/checkout/orders", json=payload, headers={"Prefer": "return=representation"}
    )
    return parse_order(res)


async def capture_order(client: PayPalClient, order_id: str) -> OrderResult:
    res = await client.request(
        "POST", f"/v2/checkout/orders/{order_id}/capture", headers={"Prefer": "return=representation"}
    )
    result = parse_order(res)
    result.order_id = result.order_id or order_id
    return result


async def refund_capture(client: PayPalClient, capture_id: str, note: str) -> PayPalResponse:
    return await client.request(
        "POST", f"/v2/payments/captures/{capture_id}/refund", json={"note_to_payer": note[:255]}
    )


async def generate_client_token(client: PayPalClient) -> str | None:
    """Browser-safe token for JS SDK v6 (`/v1/oauth2/token` with response_type=client_token)."""
    s = client.settings
    res = await client.raw_post_form(
        "/v1/oauth2/token",
        data={"grant_type": "client_credentials", "response_type": "client_token", "intent": "sdk_init"},
        auth=(s.paypal_client_id, s.paypal_client_secret),
    )
    return res.get("access_token") if isinstance(res, dict) else None
