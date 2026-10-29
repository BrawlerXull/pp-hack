"""PayPal webhook signature verification.

Uses the postback API (POST /v1/notifications/verify-webhook-signature). Postback does not
work for Webhook Simulator mock events (developer.paypal.com), so those come back FAILURE.
TODO(P2): add offline self-verification (CRC32 of raw body + cert), which PayPal recommends.
"""

import json
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

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
    BAD_CERT_URL = "BAD_CERT_URL"
    ERROR = "ERROR"
    # Not from PayPal: an event the simulator's observer mode wrote straight into the table (never through
    # the HTTP endpoint, which always verifies). Processed like a verified one, labelled simulated.
    SIMULATED = "SIMULATED"
    # Read by our own authenticated call to PayPal's Disputes API (app/disputes/sync.py), not pushed to us.
    API_SYNC = "API_SYNC"


# Verifications whose events may change state (webhook_processor.py).
TRUSTED = frozenset({Verification.SUCCESS, Verification.SIMULATED, Verification.API_SYNC})


def cert_url_ok(url: str | None) -> bool:
    """PayPal signs with a certificate it serves itself: https on paypal.com. Anything else is not PayPal,
    so it is refused here without asking PayPal (defence in depth: the postback checks the cert too)."""
    if not url:
        return False
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and (host == "paypal.com" or host.endswith(".paypal.com"))


# Buyer personal data PayPal includes in events. Nothing Checkout Guard does reads it (the processor uses
# ids, amounts, statuses and payer_id), so it is never stored.
PII_KEYS = frozenset(
    {
        "name",
        "given_name",
        "surname",
        "full_name",
        "email_address",
        "email",
        "phone",
        "phone_number",
        "national_number",
        "address",
        "shipping",
        "birth_date",
        "tax_info",
    }
)
REDACTED = "[redacted]"


def safe_signals(event: dict[str, Any]) -> dict[str, Any]:
    """What detection needs from the buyer data, taken before redaction: the email's domain (never the
    address), the card's brand, BIN and last 4 (never the number), and a hash of the payer id."""
    import hashlib

    r = event.get("resource") or {}
    payer = r.get("payer") or {}
    email = payer.get("email_address")
    card = (r.get("payment_source") or {}).get("card") or {}
    payer_id = payer.get("payer_id")
    domain = (
        email.rsplit("@", 1)[1].strip().lower()[:255] if isinstance(email, str) and "@" in email else None
    )
    out = {
        "email_domain": domain,
        "payer_id_hash": hashlib.sha256(payer_id.encode()).hexdigest()[:32]
        if isinstance(payer_id, str)
        else None,
        "card_brand": card.get("brand"),
        "card_last4": card.get("last_digits"),
        "card_bin6": (card.get("bin_details") or {}).get("bin"),
        "card_expiry": card.get("expiry"),
    }
    return {k: v for k, v in out.items() if v}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: REDACTED if k in PII_KEYS else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


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
    lowered = {k.lower(): v for k, v in headers.items()}
    body = build_verify_body(lowered, webhook_id, raw_body)
    if body is None:
        return Verification.MISSING_HEADERS
    if not cert_url_ok(lowered.get(HEADER_MAP["cert_url"])):
        return Verification.BAD_CERT_URL
    try:
        res = await client.request("POST", "/v1/notifications/verify-webhook-signature", content=body)
    except Exception:
        return Verification.ERROR
    if not res.ok or not isinstance(res.json, dict):
        return Verification.ERROR
    return Verification.SUCCESS if res.json.get("verification_status") == "SUCCESS" else Verification.FAILURE
