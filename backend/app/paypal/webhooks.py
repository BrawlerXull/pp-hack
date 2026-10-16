"""PayPal webhook signature verification.

Uses the postback API (POST /v1/notifications/verify-webhook-signature). Postback does not
work for Webhook Simulator mock events (developer.paypal.com), so those come back FAILURE.
TODO(P2): add offline self-verification (CRC32 of raw body + cert), which PayPal recommends.
"""

import json
from enum import StrEnum

from app.paypal.client import PayPalClient

HEADER_MAP = {
    "auth_algo": "paypal-auth-algo",
    "cert_url": "paypal-cert-url",
    "transmission_id": "paypal-transmission-id",
    "transmission_sig": "paypal-transmission-sig",
    "transmission_time": "paypal-transmission-time",
}


class Verification(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    MISSING_HEADERS = "MISSING_HEADERS"
    NO_WEBHOOK_ID = "NO_WEBHOOK_ID"
    ERROR = "ERROR"


def build_verify_body(headers: dict[str, str], webhook_id: str, raw_body: str) -> str | None:
    """Build the postback body. `webhook_event` must be the event exactly as received,
    so the raw body is spliced in rather than re-serialised."""
    fields: dict[str, str] = {}
    for key, header in HEADER_MAP.items():
        value = headers.get(header)
        if not value:
            return None
        fields[key] = value
    fields["webhook_id"] = webhook_id
    prefix = json.dumps(fields)[:-1]  # drop closing brace
    return f'{prefix}, "webhook_event": {raw_body}}}'


async def verify_webhook(
    client: PayPalClient, headers: dict[str, str], raw_body: str, webhook_id: str
) -> Verification:
    if not webhook_id:
        return Verification.NO_WEBHOOK_ID
    body = build_verify_body({k.lower(): v for k, v in headers.items()}, webhook_id, raw_body)
    if body is None:
        return Verification.MISSING_HEADERS
    try:
        res = await client.request("POST", "/v1/notifications/verify-webhook-signature", content=body)
    except Exception:
        return Verification.ERROR
    if not res.ok or not isinstance(res.json, dict):
        return Verification.ERROR
    return Verification.SUCCESS if res.json.get("verification_status") == "SUCCESS" else Verification.FAILURE
