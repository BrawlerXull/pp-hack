import hmac
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.config import Settings, get_settings
from app.events import Broadcaster
from app.paypal.client import PayPalClient

SESSION_COOKIE = "cg_session"
SESSION_MAX_AGE_S = 7 * 24 * 3600


def get_paypal(request: Request) -> PayPalClient:
    return request.app.state.paypal


def get_broadcaster(request: Request) -> Broadcaster:
    return request.app.state.broadcaster


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_secret, salt="cg-owner")


def issue_session(settings: Settings) -> str:
    return _serializer(settings).dumps({"role": "owner", "shop": settings.shop_id})


def require_owner(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """Dashboard auth: signed session cookie (browser) or `Authorization: Bearer <token>` (CLI)."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:]
    if not token:
        raise HTTPException(401, "login required")
    try:
        data = _serializer(settings).loads(token, max_age=SESSION_MAX_AGE_S)
    except BadSignature as exc:
        raise HTTPException(401, "invalid session") from exc
    return data["shop"]


def require_shop_key(
    settings: Annotated[Settings, Depends(get_settings)],
    x_guard_key: Annotated[str | None, Header()] = None,
) -> str:
    """External guard API auth for shops calling /v1/guard/* from their own checkout."""
    if not x_guard_key or not hmac.compare_digest(x_guard_key, settings.shop_api_key):
        raise HTTPException(401, "invalid X-Guard-Key")
    return settings.shop_id


def require_simulator(settings: Annotated[Settings, Depends(get_settings)]) -> None:
    if not settings.simulator_enabled:
        raise HTTPException(404, "simulator disabled")


OwnerShop = Annotated[str, Depends(require_owner)]
