"""P1 sandbox spike: run a small scripted card-testing attack against the PayPal sandbox and
record what actually comes back (order states, decline codes, 3DS behaviour, refunds).

SIMULATION ONLY: uses PayPal sandbox test cards, never real card data.

    uv run python scripts/spike.py               # 20 attempts + refunds
    uv run python scripts/spike.py --count 100   # bigger burst
    uv run python scripts/spike.py --sca         # also the 3-D Secure card matrix

Results go to spike-output/<run>.jsonl; summarise them in docs/sandbox-findings.md.
"""

import asyncio
import json
import random
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from app.config import get_settings
from app.paypal.client import PayPalClient, PayPalResponse

# developer.paypal.com/api/rest/sandbox/card-testing and the 3DS test-card page
APPROVE_CARD = "4012888888881881"
# Case-sensitive triggers in the name-on-card field that force declines.
DECLINE_TRIGGERS = [
    "CCREJECT-REFUSED",
    "CCREJECT-SF",
    "CCREJECT-EC",
    "CCREJECT-IF",
    "CCREJECT-LS",
    "CCREJECT-CVV_F",
]
SCA_CARDS = {
    "challenge_success": "4868719166101368",
    "challenge_fail": "4868719181895556",
    "frictionless_success": "4868719196829038",
    "frictionless_fail": "4868719158130060",
}

app = typer.Typer(add_completion=False)


class Recorder:
    def __init__(self, out_dir: str) -> None:
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        self.path = Path(out_dir) / f"spike-{run_id}.jsonl"
        self.run_id = run_id

    def write(self, entry: dict[str, Any]) -> None:
        with self.path.open("a") as f:
            f.write(json.dumps({"ts": datetime.now(UTC).isoformat(), **entry}) + "\n")


def first_capture(res: PayPalResponse | None) -> dict[str, Any] | None:
    if not res or not isinstance(res.json, dict):
        return None
    units = res.json.get("purchase_units") or [{}]
    captures = (units[0].get("payments") or {}).get("captures") or []
    return captures[0] if captures else None


async def attempt(
    pp: PayPalClient, rec: Recorder, label: str, card: str, name: str, sca: str | None = None
) -> dict[str, Any]:
    card_source: dict[str, Any] = {"number": card, "expiry": "2030-12", "security_code": "123", "name": name}
    if sca:
        card_source["attributes"] = {"verification": {"method": sca}}
        # Without return/cancel URLs the payer-action (3DS) page has nowhere to send the buyer back.
        app_url = pp.settings.public_app_url.rstrip("/")
        card_source["experience_context"] = {
            "return_url": f"{app_url}/?3ds=return",
            "cancel_url": f"{app_url}/?3ds=cancel",
        }

    create = await pp.request(
        "POST",
        "/v2/checkout/orders",
        headers={"Prefer": "return=representation"},
        json={
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "reference_id": "SKU-TEALIGHT-3",
                    "description": "Tea-light pack (spike test)",
                    "custom_id": f"spike-{rec.run_id}",
                    "amount": {"currency_code": "USD", "value": "3.00"},
                }
            ],
            "payment_source": {"card": card_source},
        },
    )
    body = create.json if isinstance(create.json, dict) else {}
    order_id = body.get("id")
    order_status = body.get("status")

    # Card orders come back APPROVED (capture needed) or COMPLETED; 3DS gives PAYER_ACTION_REQUIRED.
    capture = None
    if order_id and order_status == "APPROVED":
        capture = await pp.request(
            "POST", f"/v2/checkout/orders/{order_id}/capture", headers={"Prefer": "return=representation"}
        )
        if isinstance(capture.json, dict):
            order_status = capture.json.get("status", order_status)

    cap = first_capture(capture) or first_capture(create)
    result = {
        "kind": "attempt",
        "label": label,
        "email_domain": f"throwaway-{random.randint(0, 999)}.test",
        "sca": sca,
        "create_http": create.status,
        "create_ms": create.elapsed_ms,
        "create_debug_id": create.debug_id,
        "create_error": create.error_summary(),
        "order_id": order_id,
        "order_status": order_status,
        "payer_action_link": next(
            (link["href"] for link in body.get("links", []) if link.get("rel") == "payer-action"), None
        ),
        "capture_http": capture.status if capture else None,
        "capture_error": capture.error_summary() if capture else None,
        "capture_id": cap.get("id") if cap else None,
        "capture_status": cap.get("status") if cap else None,
        "processor_response": cap.get("processor_response") if cap else None,
        "auth_result": ((body.get("payment_source") or {}).get("card") or {}).get("authentication_result"),
    }
    rec.write(result)
    return result


async def refund(pp: PayPalClient, rec: Recorder, capture_id: str) -> dict[str, Any]:
    res = await pp.request(
        "POST",
        f"/v2/payments/captures/{capture_id}/refund",
        json={"note_to_payer": "Refunded by Checkout Guard spike test"},
    )
    body = res.json if isinstance(res.json, dict) else {}
    out = {
        "kind": "refund",
        "capture_id": capture_id,
        "http": res.status,
        "refund_id": body.get("id"),
        "refund_status": body.get("status"),
        "error": res.error_summary(),
    }
    rec.write(out)
    return out


def issues(r: dict[str, Any]) -> str:
    err = r.get("create_error") or r.get("capture_error") or {}
    return ",".join(i for i in err.get("issues", []) if i)


async def run(count: int, sca: bool, delay_ms: int) -> None:
    settings = get_settings()
    pp = PayPalClient(settings)
    rec = Recorder(settings.spike_output_dir)
    print(f"Spike run {rec.run_id} → {rec.path}")
    try:
        # 1. Card-testing burst: ~30% approvals, the rest declines with varied reasons.
        results = []
        for i in range(count):
            if random.random() < 0.3:
                r = await attempt(pp, rec, f"burst-{i}-approve", APPROVE_CARD, "Test Buyer")
            else:
                trigger = DECLINE_TRIGGERS[i % len(DECLINE_TRIGGERS)]
                r = await attempt(pp, rec, f"burst-{i}-decline", APPROVE_CARD, trigger)
            results.append(r)
            print(
                f"{r['label']:<22} create={r['create_http']} order={r['order_status'] or '-'} "
                f"capture={r['capture_status'] or '-'} {issues(r)}"
            )
            await asyncio.sleep(delay_ms / 1000)  # stay well under sandbox rate limits

        # 2. Refund the successful captures: proves the Refunds API path the Guard will use.
        captured = [r for r in results if r["capture_status"] == "COMPLETED" and r["capture_id"]]
        for r in captured:
            rf = await refund(pp, rec, r["capture_id"])
            print(f"refund {r['capture_id']} → {rf['http']} {rf['refund_status'] or rf['error']}")

        # 3. Optional 3-D Secure matrix: does SCA_ALWAYS force payer action for each card?
        if sca:
            for label, card in SCA_CARDS.items():
                for method in ("SCA_WHEN_REQUIRED", "SCA_ALWAYS"):
                    r = await attempt(pp, rec, f"3ds-{label}-{method}", card, "Test Buyer", method)
                    print(
                        f"{r['label']:<42} create={r['create_http']} order={r['order_status'] or '-'} "
                        f"payer_action={'yes' if r['payer_action_link'] else 'no'} {issues(r)}"
                    )

        summary = Counter(
            r["capture_status"] or r["order_status"] or issues(r) or f"HTTP_{r['create_http']}"
            for r in results
        )
        print("\nOutcome summary:", dict(summary))
        print(f"Captured and refunded: {len(captured)}. Raw log: {rec.path}")
        print("Check spike-output/webhooks.jsonl for the webhooks PayPal sent.")
    finally:
        await pp.aclose()


@app.command()
def main(
    count: int = typer.Option(20, help="Attempts in the burst"),
    sca: bool = typer.Option(False, help="Also run the 3-D Secure test-card matrix"),
    delay_ms: int = typer.Option(300, help="Pause between attempts"),
) -> None:
    asyncio.run(run(count, sca, delay_ms))


if __name__ == "__main__":
    app()
