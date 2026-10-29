"""The demo shop's own server (what a merchant's checkout backend would do).

Device identity: the first checkout call from a browser gets a signed cookie holding its device id, and
every later call is keyed on the cookie, whatever device id the body claims. A bot that keeps its cookies
can no longer rotate ids; one that drops them looks like a new device each time, which the guard's
network rule catches (app/guard/check.py). Callers without cookies (API clients) are keyed on their id.
"""

from typing import Annotated, Literal

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from itsdangerous import BadSignature, Signer
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_paypal
from app.api.ratelimit import limit
from app.config import Settings, get_settings
from app.db.models import CheckoutAttempt, Decision
from app.db.session import get_session
from app.guard import signals
from app.guard.check import GuardContext, check_within_budget, issue_challenge_token
from app.paypal.client import PayPalClient
from app.paypal.orders import generate_client_token
from app.shop.catalog import PRODUCTS
from app.shop.service import CheckoutRequest, CheckoutResult, capture_web_order, fingerprint, place_order

log = structlog.get_logger()
router = APIRouter(prefix="/shop", tags=["shop"])
DEVICE_COOKIE = "cg_dev"
DEVICE_COOKIE_MAX_AGE_S = 365 * 24 * 3600
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
TURNSTILE_TEST_SITE_KEYS = {
    "1x00000000000000000000AA",  # always passes
    "2x00000000000000000000AB",  # always blocks
    "3x00000000000000000000FF",  # forces an interactive challenge
}
Session = Annotated[AsyncSession, Depends(get_session)]
PayPal = Annotated[PayPalClient, Depends(get_paypal)]


class ProductOut(BaseModel):
    sku: str
    name: str
    price: str
    description: str


class OrderIn(BaseModel):
    sku: str
    method: Literal["card", "wallet"]
    device_id: str = Field(
        min_length=6, max_length=80, description="Random id the browser keeps in localStorage"
    )
    email: str | None = Field(default=None, max_length=254)
    challenge_token: str | None = None
    # FraudNet correlation id from the checkout page (32 hex characters).
    client_metadata_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9]{8,36}$")


class PrecheckIn(BaseModel):
    sku: str
    method: Literal["card", "wallet"]
    device_id: str = Field(min_length=6, max_length=80)
    challenge_token: str | None = None


class ChallengeIn(BaseModel):
    device_id: str = Field(min_length=6, max_length=80)
    # Cloudflare Turnstile's token from the widget; required when TURNSTILE_SECRET_KEY is set.
    turnstile_token: str | None = Field(default=None, max_length=4096)


def client_ip(request: Request) -> str | None:
    """Counted from the right of X-Forwarded-For (app/guard/signals.py): the left end is client-written."""
    return signals.request_ip(request)


def _device_signer(settings: Settings) -> Signer:
    return Signer(settings.session_secret, salt="cg-device")


def device_id(request: Request, response: Response, claimed: str, settings: Settings) -> str:
    """The cookie's device id when it carries a valid one; else the claimed id, now pinned in a cookie."""
    raw = request.cookies.get(DEVICE_COOKIE)
    if raw:
        try:
            return _device_signer(settings).unsign(raw).decode()
        except BadSignature:
            pass
    response.set_cookie(
        DEVICE_COOKIE,
        _device_signer(settings).sign(claimed).decode(),
        max_age=DEVICE_COOKIE_MAX_AGE_S,
        httponly=True,
        samesite="lax",
        secure=settings.environment == "production",
    )
    return claimed


@router.get("/products")
def products() -> list[ProductOut]:
    return [
        ProductOut(sku=p.sku, name=p.name, price=f"{p.price:.2f}", description=p.description)
        for p in PRODUCTS.values()
    ]


@router.get("/config")
def config(settings: Annotated[Settings, Depends(get_settings)]) -> dict:
    # The client id and the Turnstile site key are public by design (they are in the page's scripts).
    return {
        "paypal_client_id": settings.paypal_client_id,
        "environment": "sandbox",
        "currency": "USD",
        "turnstile_site_key": settings.turnstile_site_key if settings.turnstile_secret_key else "",
        "turnstile_test_keys": settings.turnstile_site_key in TURNSTILE_TEST_SITE_KEYS,
        "contact_email": settings.shop_contact_email,
        "fraudnet_source_id": settings.paypal_fraudnet_source_id,
    }


@router.post("/client-token", dependencies=[Depends(limit("client-token", 20, 60))])
async def client_token(paypal: PayPal) -> dict:
    token = await generate_client_token(paypal)
    if not token:
        raise HTTPException(502, "could not get a PayPal client token")
    return {"client_token": token}


@router.post("/orders", dependencies=[Depends(limit("orders", 30, 60))])
async def create(
    body: OrderIn,
    request: Request,
    response: Response,
    session: Session,
    paypal: PayPal,
    settings: Annotated[Settings, Depends(get_settings)],
) -> CheckoutResult:
    if body.sku not in PRODUCTS:
        raise HTTPException(404, "unknown product")
    result = await place_order(
        session,
        paypal,
        CheckoutRequest(
            shop_id=settings.shop_id,
            sku=body.sku,
            method=body.method,
            device_id=device_id(request, response, body.device_id, settings),
            user_agent=request.headers.get("user-agent"),
            ip=client_ip(request),
            email=body.email,
            challenge_token=body.challenge_token,
            client_metadata_id=body.client_metadata_id,
        ),
    )
    if result.decision == Decision.BLOCK:
        response.status_code = 403
    elif result.decision == Decision.CHALLENGE:
        response.status_code = 428  # Precondition Required: complete the challenge, then retry
    return result


@router.post("/orders/{order_id}/capture")
async def capture(
    order_id: str, session: Session, paypal: PayPal, settings: Annotated[Settings, Depends(get_settings)]
) -> dict:
    """Capture after PayPal approval (button) or Card Fields submit. Card orders are captured only when
    their 3-D Secure result allows it; outcome `sca_failed` means the bank's check did not pass."""
    attempt = await capture_web_order(session, paypal, settings.shop_id, order_id)
    return _capture_answer(order_id, attempt)


@router.post("/orders/{order_id}/card-result")
async def card_result(
    order_id: str, session: Session, paypal: PayPal, settings: Annotated[Settings, Depends(get_settings)]
) -> dict:
    """Card Fields reported a failed or cancelled submit: record the bank's answer, never capture. Without
    this the row would stay CREATED and the dashboard would miss real 3-D Secure failures."""
    attempt = await capture_web_order(session, paypal, settings.shop_id, order_id, record_only=True)
    return _capture_answer(order_id, attempt)


def _capture_answer(order_id: str, attempt: CheckoutAttempt | None) -> dict:
    if attempt is None:
        raise HTTPException(404, "unknown order")
    return {
        "order_id": order_id,
        "outcome": attempt.outcome,
        "capture_id": attempt.capture_id,
        "decline_code": attempt.decline_code,
        "decline_category": attempt.decline_category,
        "liability_shift": attempt.sca_liability_shift,
    }


@router.post("/challenge", dependencies=[Depends(limit("challenge", 10, 60))])
async def challenge(
    body: ChallengeIn,
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    """Human check → short-lived token bound to this device fingerprint. With TURNSTILE_SECRET_KEY set (the
    default is Cloudflare's always-pass test key) the Turnstile token is verified with Cloudflare first;
    without one it is the old click-through, and the answer says so."""
    verified_by = "click_through"
    if settings.turnstile_secret_key:
        token = body.turnstile_token
        if not token or not await _turnstile_ok(token, client_ip(request), settings):
            failed = {"code": "human_check_failed", "message": "The human check did not pass."}
            raise HTTPException(403, failed)
        test_keys = settings.turnstile_site_key in TURNSTILE_TEST_SITE_KEYS
        verified_by = "turnstile_test" if test_keys else "turnstile"
    device = device_id(request, response, body.device_id, settings)
    fp = fingerprint(device, request.headers.get("user-agent"))
    return {"challenge_token": issue_challenge_token(fp), "expires_in": 600, "verified_by": verified_by}


async def _turnstile_ok(token: str, ip: str | None, settings: Settings) -> bool:
    data = {"secret": settings.turnstile_secret_key, "response": token, **({"remoteip": ip} if ip else {})}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.post(TURNSTILE_VERIFY_URL, data=data)
        ok = bool(res.json().get("success"))
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("turnstile_verify_failed", error=type(exc).__name__)
        return False
    if not ok:
        log.info("turnstile_rejected", codes=res.json().get("error-codes"))
    return ok


@router.post("/precheck", dependencies=[Depends(limit("precheck", 60, 60))])
async def precheck(
    body: PrecheckIn,
    request: Request,
    response: Response,
    session: Session,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    """Ask the guard before showing the PayPal button, so a challenge is shown up front instead of
    after PayPal's popup has opened. Not logged as an attempt; /shop/orders enforces the same check."""
    fp = fingerprint(
        device_id(request, response, body.device_id, settings), request.headers.get("user-agent")
    )
    net = signals.ip_net_hash(client_ip(request))
    ctx = GuardContext(settings.shop_id, body.sku, body.method, fp, body.challenge_token, net)
    v = await check_within_budget(session, ctx, lock=False)
    return {"decision": v.decision, "reason": v.reason}
