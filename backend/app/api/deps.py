import hmac
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.ext.asyncio import AsyncSession

from app import visitor
from app.config import Settings, get_settings
from app.db.models import Role, User
from app.db.session import get_session
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


def issue_session(settings: Settings, user: User) -> str:
    """A signed session for one member. The role is looked up again on every request, so a change by an
    owner (or a removal) takes effect at once, not when the session expires."""
    return _serializer(settings).dumps({"uid": user.id, "shop": user.shop_id})


async def require_owner(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """Dashboard auth for any signed-in member: signed session cookie (browser) or `Authorization: Bearer
    <token>` (CLI). Returns the shop id; the member's id and role go to app/visitor.py."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:]
    if not token:
        raise HTTPException(401, "login required")
    try:
        data = _serializer(settings).loads(token, max_age=SESSION_MAX_AGE_S)
    except BadSignature as exc:
        raise HTTPException(401, "invalid session") from exc
    user = await session.get(User, data.get("uid") or "")
    if user is None:  # removed from the team, or a session from before accounts existed
        raise HTTPException(401, "login required")
    visitor.set_current(user.id, user.role)  # async dependency: the endpoint runs in this same context
    return user.shop_id


async def require_manager(shop_id: Annotated[str, Depends(require_owner)]) -> str:
    """Settings, approvals, the kill switch and the team: owners only. Viewers are refused; the role is unset
    only when a test overrides require_owner."""
    if visitor.role() == Role.VIEWER:
        raise HTTPException(403, "Only an owner can do this. Ask an owner to change your role.")
    return shop_id


async def current_user(
    _: Annotated[str, Depends(require_owner)], session: Annotated[AsyncSession, Depends(get_session)]
) -> User:
    user = await session.get(User, visitor.current() or "")
    if user is None:
        raise HTTPException(401, "login required")
    return user


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
ManagerShop = Annotated[str, Depends(require_manager)]
Member = Annotated[User, Depends(current_user)]
