"""Register (or update) the sandbox webhook for Checkout Guard and save its ID to .env.

    uv run python scripts/register_webhook.py https://<your-ngrok-or-render-host>

Equivalent to Developer Dashboard → Apps & Credentials → (your sandbox app) → Sandbox Webhooks →
Add Webhook, with the URL <host>/webhooks/paypal and the event types below.
"""

import asyncio
import re
from pathlib import Path

import typer

from app.config import get_settings
from app.paypal.client import PayPalClient

EVENT_TYPES = [
    "CHECKOUT.ORDER.APPROVED",
    "CHECKOUT.ORDER.COMPLETED",
    "PAYMENT.CAPTURE.COMPLETED",
    "PAYMENT.CAPTURE.DECLINED",
    "PAYMENT.CAPTURE.DENIED",
    "PAYMENT.CAPTURE.PENDING",
    "PAYMENT.CAPTURE.REFUNDED",
    "PAYMENT.CAPTURE.REVERSED",
    "CUSTOMER.DISPUTE.CREATED",
    "CUSTOMER.DISPUTE.UPDATED",
    "CUSTOMER.DISPUTE.RESOLVED",
]
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def save_webhook_id(webhook_id: str) -> None:
    text = ENV_FILE.read_text() if ENV_FILE.exists() else ""
    if re.search(r"(?m)^PAYPAL_WEBHOOK_ID=.*$", text):
        text = re.sub(r"(?m)^PAYPAL_WEBHOOK_ID=.*$", f"PAYPAL_WEBHOOK_ID={webhook_id}", text)
    else:
        text += f"\nPAYPAL_WEBHOOK_ID={webhook_id}\n"
    ENV_FILE.write_text(text)


async def run(base_url: str) -> None:
    url = base_url.rstrip("/") + "/webhooks/paypal"
    if not url.startswith("https://"):
        raise typer.BadParameter("PayPal requires an https URL (use ngrok locally)")
    pp = PayPalClient(get_settings())
    try:
        existing = await pp.request("GET", "/v1/notifications/webhooks")
        hooks = (existing.json or {}).get("webhooks", []) if existing.ok else []
        events = [{"name": e} for e in EVENT_TYPES]

        same = next((h for h in hooks if h.get("url") == url), None)
        if same:
            res = await pp.request(
                "PATCH",
                f"/v1/notifications/webhooks/{same['id']}",
                json=[{"op": "replace", "path": "/event_types", "value": events}],
            )
            webhook_id = same["id"]
            issue = (res.error_summary() or {}).get("name")
            if res.ok:
                print(f"Updated existing webhook {webhook_id} → {url}")
            elif issue == "WEBHOOK_PATCH_REQUEST_NO_CHANGE":
                print(f"Webhook {webhook_id} already points at {url} with all events: nothing to change")
            else:
                raise SystemExit(f"Could not update webhook {webhook_id}: HTTP {res.status} {res.json}")
        else:
            res = await pp.request(
                "POST", "/v1/notifications/webhooks", json={"url": url, "event_types": events}
            )
            if not res.ok:
                raise SystemExit(f"Could not create webhook: HTTP {res.status} {res.json}")
            webhook_id = res.json["id"]
            print(f"Created webhook {webhook_id} → {url}")

        others = [h for h in hooks if h.get("url") != url]
        if others:
            print("Other webhooks on this app (sandbox allows up to 10):")
            for h in others:
                print(f"  {h['id']}  {h['url']}")
        save_webhook_id(webhook_id)
        print(f"Saved PAYPAL_WEBHOOK_ID to {ENV_FILE}. Restart the API so it picks it up.")
    finally:
        await pp.aclose()


def main(base_url: str = typer.Argument(..., help="Public https base URL of the backend")) -> None:
    asyncio.run(run(base_url))


if __name__ == "__main__":
    typer.run(main)
