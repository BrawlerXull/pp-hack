"""Orders v2 + Payments helpers. Pure payload builders + small result parsers, so they're easy to test."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.paypal.client import PayPalClient, PayPalResponse
from app.paypal.three_ds import CardInfo, parse_card


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
    card_fields: bool = False,
    return_url: str | None = None,
    cancel_url: str | None = None,
    hold: bool = False,
) -> dict[str, Any]:
    """`hold`: intent AUTHORIZE instead of CAPTURE (payment hold, app/actions/holds.py): the bank approves the
    payment but nothing settles until we capture the authorization, or void it.

    `card_fields`: a browser card order. It carries no card data (PayPal's hosted Card Fields send the
    number straight to PayPal on submit), only the 3-D Secure method: SCA_ALWAYS while the guard requires
    it, else SCA_WHEN_REQUIRED (the bank decides)."""
    payload: dict[str, Any] = {
        "intent": "AUTHORIZE" if hold else "CAPTURE",
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
    if card_fields:
        card_source = {
            "attributes": {"verification": {"method": "SCA_ALWAYS" if force_sca else "SCA_WHEN_REQUIRED"}}
        }
        if return_url and cancel_url:
            card_source["experience_context"] = {"return_url": return_url, "cancel_url": cancel_url}
    elif force_sca:
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
    # Intent AUTHORIZE: the authorization (payment hold) and its status (CREATED = held, DENIED = refused).
    authorization_id: str | None = None
    authorization_status: str | None = None
    # payment_source.card: brand, last 4, BIN and the 3-D Secure result (never the number).
    card: CardInfo | None = None


def parse_order(res: PayPalResponse) -> OrderResult:
    body = res.json if isinstance(res.json, dict) else {}
    units = body.get("purchase_units") or [{}]
    payments = units[0].get("payments") or {}
    captures = payments.get("captures") or []
    cap = captures[0] if captures else {}
    auths = payments.get("authorizations") or []
    auth = auths[0] if auths else {}
    err = res.error_summary() or {}
    issue = next((i for i in err.get("issues", []) if i), None) or err.get("name")
    processor_code = None
    if cap.get("status") in {"DECLINED", "FAILED"}:
        processor_code = (cap.get("processor_response") or {}).get("response_code")
    elif auth.get("status") in {"DENIED", "FAILED"}:
        processor_code = (auth.get("processor_response") or {}).get("response_code")
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
        card=parse_card(body.get("payment_source")),
        authorization_id=auth.get("id"),
        authorization_status=auth.get("status"),
    )


def order_request_id(attempt_id: str) -> str:
    return f"cg-order-{attempt_id}"


def capture_request_id(order_id: str, tries: int = 0) -> str:
    """Same key for every capture of an order whose last result is unknown (timeout, 5xx), so PayPal
    sees one request. `tries` (definitive 4xx refusals so far) changes it after a refusal: PayPal
    replays a cached response for a reused key, which would replay the refusal to the legitimate
    capture after 3-D Secure or after the buyer picked another funding source."""
    return f"cg-capture-{order_id}" + (f"-{tries}" if tries else "")


def authorize_request_id(order_id: str, tries: int = 0) -> str:
    return f"cg-authorize-{order_id}" + (f"-{tries}" if tries else "")


def hold_request_id(attempt_id: str, verb: str) -> str:
    # One key per held payment and verb: a retried capture or void of the same hold is one request at PayPal.
    return f"cg-hold-{verb}-{attempt_id}"


def refund_request_id(attempt_id: str) -> str:
    # Per attempt, not per action: two incidents (or owner + autopilot) can never refund it twice.
    return f"cg-refund-{attempt_id}"


async def create_order(
    client: PayPalClient, payload: dict[str, Any], request_id: str, client_metadata_id: str | None = None
) -> OrderResult:
    """`client_metadata_id`: the checkout page's FraudNet correlation id (PayPal-Client-Metadata-Id), so
    PayPal's own risk engine scores the order with the device data its script collected on that page."""
    headers = {"Prefer": "return=representation"}
    if client_metadata_id:
        headers["PayPal-Client-Metadata-Id"] = client_metadata_id
    res = await client.request(
        "POST", "/v2/checkout/orders", json=payload, headers=headers, request_id=request_id
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


async def authorize_order(client: PayPalClient, order_id: str, tries: int = 0) -> OrderResult:
    """An approved intent-AUTHORIZE order → a payment hold (authorization), nothing settled."""
    res = await client.request(
        "POST",
        f"/v2/checkout/orders/{order_id}/authorize",
        headers={"Prefer": "return=representation"},
        request_id=authorize_request_id(order_id, tries),
    )
    result = parse_order(res)
    result.order_id = result.order_id or order_id
    return result


async def capture_authorization(
    client: PayPalClient, authorization_id: str, request_id: str
) -> PayPalResponse:
    """Settle a payment hold. The answer is a capture (id, status), not an order."""
    return await client.request(
        "POST",
        f"/v2/payments/authorizations/{authorization_id}/capture",
        json={"final_capture": True},
        headers={"Prefer": "return=representation"},
        request_id=request_id,
    )


async def void_authorization(client: PayPalClient, authorization_id: str, request_id: str) -> PayPalResponse:
    """Release a payment hold: nothing settles, so there is no fee and nothing for anyone to dispute."""
    return await client.request(
        "POST", f"/v2/payments/authorizations/{authorization_id}/void", request_id=request_id
    )


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
