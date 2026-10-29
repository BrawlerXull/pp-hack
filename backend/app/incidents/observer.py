"""Observer mode: watch a shop's PayPal payments with no change to its checkout.

A merchant on PayPal's hosted buttons or a shop plugin cannot call the guard before each order. They connect
their PayPal account instead (its webhooks point here), and every verified order and capture event for an
order no checkout of ours created becomes an OBSERVED attempt. Detection, the verdict, refunds, the alert and
the reports then work exactly as for guarded checkouts:

  - captures and capture declines/denials (PAYMENT.CAPTURE.*) give the outcome, amount and decline code;
  - order events (CHECKOUT.ORDER.APPROVED / COMPLETED) add the item, the email domain and the card's brand
    and last 4 (app/paypal/webhooks.safe_signals: kept before the buyer's data is redacted).

PayPal does not always send the order event: for a card captured in the same call that created the order,
the sandbox sends only PAYMENT.CAPTURE.COMPLETED (seen live, Oct 29). enrich() then reads the order itself
(GET /v2/checkout/orders/{id}, the merchant's own order) for the item, the card and the buyer's domain.
Until then the payment method is "unknown", never a guess.

What observer mode cannot do is act before a payment: blocking a device, 3-D Secure, a held item and payment
holds all need the checkout to ask the guard first (POST /v1/guard/check, or the demo shop). It also has no
device fingerprint: each card (or PayPal payer) stands in for one, so refunds of observed payments match
weakly and wait for the owner. A card decline PayPal refuses at order creation sends no webhook at all, so
observer mode sees fewer declines than the guard does.
"""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import CheckoutAttempt, Decision, Method, Outcome, PayPalEvent, Source
from app.paypal import codes
from app.paypal.webhooks import Verification

CAPTURE_EVENTS = frozenset(
    {"PAYMENT.CAPTURE.COMPLETED", "PAYMENT.CAPTURE.DECLINED", "PAYMENT.CAPTURE.DENIED"}
)
ORDER_EVENTS = frozenset({"CHECKOUT.ORDER.APPROVED", "CHECKOUT.ORDER.COMPLETED"})
OBSERVABLE = CAPTURE_EVENTS | ORDER_EVENTS


def is_foreign(ref: tuple[str, str] | None, shop_id: str) -> bool:
    """Not one of our own orders: ours always carry custom_id "{shop_id}:{attempt_id}"."""
    return ref is None or ref[0] != shop_id


def _ts(r: dict[str, Any], ev: PayPalEvent) -> datetime:
    raw = r.get("create_time") or r.get("update_time")
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            pass
    return ev.received_at or datetime.now(UTC)


def _amount(r: dict[str, Any]) -> Decimal | None:
    value = (r.get("amount") or {}).get("value") or next(
        ((u.get("amount") or {}).get("value") for u in r.get("purchase_units") or []), None
    )
    try:
        return Decimal(str(value)) if value is not None else None
    except InvalidOperation:
        return None


def _item(r: dict[str, Any], amount: Decimal | None) -> str:
    """The item: its SKU when the order lists one, else the purchase unit's reference, else the price. A card
    tester hammers the cheapest item, so even "price $3.00" concentrates the way an item would."""
    for unit in r.get("purchase_units") or []:
        for item in unit.get("items") or []:
            if item.get("sku"):
                return str(item["sku"])[:64]
        if unit.get("reference_id") and unit["reference_id"] != "default":
            return str(unit["reference_id"])[:64]
    return f"price {amount:.2f}" if amount is not None else "unknown"


def _device(signals: dict[str, Any], order_id: str | None) -> str:
    """No device fingerprint without the checkout hook: the card (or PayPal payer) stands in for one."""
    import hashlib

    if signals.get("card_last4"):
        key = f"card:{signals.get('card_brand')}:{signals.get('card_bin6')}:{signals['card_last4']}"
    elif signals.get("payer_id_hash"):
        key = f"payer:{signals['payer_id_hash']}"
    else:
        key = f"order:{order_id}"
    return hashlib.sha256(f"observed|{key}".encode()).hexdigest()[:32]


def _apply_order(
    a: CheckoutAttempt, r: dict[str, Any], signals: dict[str, Any], amount: Decimal | None
) -> None:
    """What the order knows: the item, the buyer's domain and the card (the capture event may come first).
    Marks the attempt as read (order_status), so enrich() does not fetch it again."""
    if a.sku.startswith("price ") or a.sku == "unknown":
        a.sku = _item(r, amount or a.amount)
    a.email_domain = a.email_domain or signals.get("email_domain")
    a.payer_id_hash = a.payer_id_hash or signals.get("payer_id_hash")
    if signals.get("card_last4"):
        a.method = Method.CARD
        a.card_brand, a.card_last4 = signals.get("card_brand"), signals.get("card_last4")
        a.card_bin6 = a.card_bin6 or signals.get("card_bin6")
        a.card_expiry = a.card_expiry or signals.get("card_expiry")
    elif signals.get("payer_id_hash") and a.method == "unknown":
        a.method = Method.WALLET
    if a.fingerprint_hash == _device({}, a.paypal_order_id):
        a.fingerprint_hash = _device(signals, a.paypal_order_id)  # the card or payer, now that it is known
    a.order_status = r.get("status") or "READ"


ENRICH_PER_PASS = 20


async def enrich(session: AsyncSession, paypal: Any, shop_id: str) -> int:
    """Read the order behind each live observed payment PayPal only sent a capture event for (the merchant's
    own order, GET /v2/checkout/orders/{id}). Commits; returns how many were read."""
    from app.paypal.orders import get_order_json
    from app.paypal.webhooks import safe_signals

    rows = (
        (
            await session.execute(
                select(CheckoutAttempt)
                .where(
                    CheckoutAttempt.shop_id == shop_id,
                    CheckoutAttempt.source == Source.OBSERVED,
                    CheckoutAttempt.paypal_live.is_(True),
                    CheckoutAttempt.order_status.is_(None),
                    CheckoutAttempt.paypal_order_id.is_not(None),
                )
                .limit(ENRICH_PER_PASS)
            )
        )
        .scalars()
        .all()
    )
    read = 0
    for a in rows:
        status, order = await get_order_json(paypal, a.paypal_order_id or "")
        if status == 200 and isinstance(order, dict):
            _apply_order(a, order, safe_signals({"resource": order}), a.amount)
            read += 1
        elif 400 <= status < 500 and status != 429:
            a.order_status = f"UNREADABLE_{status}"  # not ours to read, or gone: do not ask again
    if rows:
        await session.commit()
    return read


async def observe(
    session: AsyncSession, ev: PayPalEvent, event_type: str, r: dict[str, Any]
) -> CheckoutAttempt:
    """Create (or complete) the OBSERVED attempt for a foreign order's event. Caller commits."""
    signals: dict[str, Any] = (ev.payload or {}).get("_cg") or {}
    related = ((r.get("supplementary_data") or {}).get("related_ids") or {}).get("order_id")
    order_id = r.get("id") if event_type in ORDER_EVENTS else related
    a = None
    if order_id:
        a = (
            await session.execute(
                select(CheckoutAttempt).where(
                    CheckoutAttempt.paypal_order_id == order_id, CheckoutAttempt.source == Source.OBSERVED
                )
            )
        ).scalar_one_or_none()
    amount = _amount(r)
    if a is None:
        a = CheckoutAttempt(
            shop_id=get_settings().shop_id,
            ts=_ts(r, ev),
            source=Source.OBSERVED,
            paypal_live=ev.verified == Verification.SUCCESS,
            sku=_item(r, amount),
            amount=amount or Decimal("0"),
            # Card or PayPal wallet: known from the order (event or enrich()), never guessed from a capture.
            method=Method.CARD if signals.get("card_last4") else "unknown",
            fingerprint_hash=_device(signals, order_id),
            guard_decision=Decision.ALLOW,
            guard_reason="observed",  # no guard check ran: seen through PayPal's webhooks only
            outcome=Outcome.CREATED,
            paypal_order_id=order_id,
        )
        session.add(a)
    if event_type in ORDER_EVENTS:
        _apply_order(a, r, signals, amount)
    elif event_type == "PAYMENT.CAPTURE.COMPLETED":
        if a.outcome != Outcome.REFUNDED:
            a.outcome, a.capture_id = Outcome.CAPTURED, r.get("id")
    else:  # declined or denied
        processor_code = (r.get("processor_response") or {}).get("response_code")
        c = codes.decline_for_webhook(processor_code, event_type.rsplit(".", 1)[-1])
        a.outcome = Outcome.DECLINED
        a.decline_code, a.decline_category, a.processor_code = c.decline_code, c.category, processor_code
    await session.flush()
    return a
