"""The demo shop's own server (what a merchant's checkout backend would do)."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_paypal
from app.config import Settings, get_settings
from app.db.models import CheckoutAttempt, Decision
from app.db.session import get_session
from app.guard.check import GuardContext, check, issue_challenge_token
from app.paypal.client import PayPalClient
from app.paypal.orders import generate_client_token
from app.shop.catalog import PRODUCTS
from app.shop.service import CheckoutRequest, CheckoutResult, capture_web_order, fingerprint, place_order

router = APIRouter(prefix="/shop", tags=["shop"])
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


class PrecheckIn(BaseModel):
    sku: str
    method: Literal["card", "wallet"]
    device_id: str = Field(min_length=6, max_length=80)
    challenge_token: str | None = None


class ChallengeIn(BaseModel):
    device_id: str = Field(min_length=6, max_length=80)


def client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else None)


@router.get("/products")
def products() -> list[ProductOut]:
    return [
        ProductOut(sku=p.sku, name=p.name, price=f"{p.price:.2f}", description=p.description)
        for p in PRODUCTS.values()
    ]


@router.get("/config")
def config(settings: Annotated[Settings, Depends(get_settings)]) -> dict:
    # The client id is public by design (it's in every PayPal button's script URL).
    return {"paypal_client_id": settings.paypal_client_id, "environment": "sandbox", "currency": "USD"}


@router.post("/client-token")
async def client_token(paypal: PayPal) -> dict:
    token = await generate_client_token(paypal)
    if not token:
        raise HTTPException(502, "could not get a PayPal client token")
    return {"client_token": token}


@router.post("/orders")
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
            device_id=body.device_id,
            user_agent=request.headers.get("user-agent"),
            ip=client_ip(request),
            email=body.email,
            challenge_token=body.challenge_token,
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


@router.post("/challenge")
def challenge(body: ChallengeIn, request: Request) -> dict:
    """Human check passed in the browser → short-lived token bound to this device fingerprint."""
    fp = fingerprint(body.device_id, request.headers.get("user-agent"))
    return {"challenge_token": issue_challenge_token(fp), "expires_in": 600}


@router.post("/precheck")
async def precheck(
    body: PrecheckIn,
    request: Request,
    session: Session,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    """Ask the guard before showing the PayPal button, so a challenge is shown up front instead of
    after PayPal's popup has opened. Not logged as an attempt; /shop/orders enforces the same check."""
    fp = fingerprint(body.device_id, request.headers.get("user-agent"))
    v = await check(session, GuardContext(settings.shop_id, body.sku, body.method, fp, body.challenge_token))
    return {"decision": v.decision, "reason": v.reason}
