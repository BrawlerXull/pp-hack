"""Orders v2 + Payments helpers. Pure payload builders + small result parsers, so they're easy to test."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.paypal.client import PayPalClient, PayPalResponse


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
    return_url: str | None = None,
    cancel_url: str | None = None,
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
        if return_url and cancel_url:
            # Where PayPal sends the buyer back after the 3-D Secure page (payer-action link).
            card_source["experience_context"] = {"return_url": return_url, "cancel_url": cancel_url}
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
    processor_code: str | None = None  # processor_response.response_code of a declined capture


def parse_order(res: PayPalResponse) -> OrderResult:
    body = res.json if isinstance(res.json, dict) else {}
    units = body.get("purchase_units") or [{}]
    captures = (units[0].get("payments") or {}).get("captures") or []
    cap = captures[0] if captures else {}
    err = res.error_summary() or {}
    issue = next((i for i in err.get("issues", []) if i), None) or err.get("name")
    processor_code = None
    if cap.get("status") in {"DECLINED", "FAILED"}:
        processor_code = (cap.get("processor_response") or {}).get("response_code")
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
        processor_code=processor_code,
    )


def order_request_id(attempt_id: str) -> str:
    return f"cg-order-{attempt_id}"


def capture_request_id(order_id: str, tries: int = 0) -> str:
    """Same key for every capture of an order whose last result is unknown (timeout, 5xx), so PayPal
    sees one request. `tries` (definitive 4xx refusals so far) changes it after a refusal: PayPal
    replays a cached response for a reused key, which would replay the refusal to the legitimate
    capture after 3-D Secure or after the buyer picked another funding source."""
    return f"cg-capture-{order_id}" + (f"-{tries}" if tries else "")


def refund_request_id(attempt_id: str) -> str:
    # Per attempt, not per action: two incidents (or owner + autopilot) can never refund it twice.
    return f"cg-refund-{attempt_id}"


async def create_order(client: PayPalClient, payload: dict[str, Any], request_id: str) -> OrderResult:
    res = await client.request(
        "POST",
        "/v2/checkout/orders",
        json=payload,
        headers={"Prefer": "return=representation"},
        request_id=request_id,
    )
    return parse_order(res)


async def capture_order(client: PayPalClient, order_id: str, tries: int = 0) -> OrderResult:
    res = await client.request(
        "POST",
        f"/v2/checkout/orders/{order_id}/capture",
        headers={"Prefer": "return=representation"},
        request_id=capture_request_id(order_id, tries),
    )
    result = parse_order(res)
    result.order_id = result.order_id or order_id
    return result


async def get_order(client: PayPalClient, order_id: str) -> OrderResult:
    result = parse_order(await client.request("GET", f"/v2/checkout/orders/{order_id}"))
    result.order_id = result.order_id or order_id
    return result


async def refund_capture(client: PayPalClient, capture_id: str, note: str, request_id: str) -> PayPalResponse:
    return await client.request(
        "POST",
        f"/v2/payments/captures/{capture_id}/refund",
        json={"note_to_payer": note[:255]},
        request_id=request_id,
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
