import hmac
import re
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import passwords
from app.api import ratelimit
from app.api.deps import SESSION_COOKIE, SESSION_MAX_AGE_S, ManagerShop, Member, issue_session
from app.api.ratelimit import limit
from app.config import Settings, get_settings
from app.db.audit import audit
from app.db.models import Role, User
from app.db.session import get_session

router = APIRouter(prefix="/auth", tags=["auth"])
Session = Annotated[AsyncSession, Depends(get_session)]
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class LoginIn(BaseModel):
    # No email: the demo account (scripts/attack.py and older clients send only the password).
    email: str | None = None
    password: str


class SignupIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)


class PasswordIn(BaseModel):
    current: str
    new: str = Field(max_length=200)


class RoleIn(BaseModel):
    role: Literal["owner", "viewer"]


def member_out(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "is_demo": user.is_demo,
        "created_at": user.created_at,
        "last_login_at": user.last_login_at,
    }


def _start_session(response: Response, settings: Settings, user: User) -> dict:
    token = issue_session(settings, user)
    # Secure in production (served over https): the cookie approves refunds and flips the kill switch.
    # Local http dev needs it off, or the browser would drop it.
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE_S,
        httponly=True,
        samesite="lax",
        secure=settings.environment == "production",
    )
    user.last_login_at = datetime.now(UTC)
    return {"ok": True, "shop_id": user.shop_id, "token": token, "user": member_out(user)}


async def _by_email(session: AsyncSession, email: str) -> User | None:
    return (
        await session.execute(select(User).where(User.email == email.strip().lower()))
    ).scalar_one_or_none()


# Password guessing: after 10 wrong passwords from one client in 5 minutes, every login from it waits (the
# right password too, or a guesser would learn it from the different answer). Only failures count, so
# judges logging in again and again never use up the allowance.
@router.post("/login", dependencies=[Depends(limit("login", 10, 300, count_all=False))])
async def login(
    body: LoginIn,
    request: Request,
    response: Response,
    session: Session,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    user = await _by_email(session, body.email or settings.demo_email)
    if user is None:
        ok = False
    elif user.is_demo:
        ok = hmac.compare_digest(body.password, settings.admin_password)
    else:
        ok = passwords.verify(body.password, user.password_hash)
    if not ok or user is None:
        ratelimit.record("login", request)
        raise HTTPException(401, "Wrong email or password")
    out = _start_session(response, settings, user)
    await session.commit()
    return out


@router.post("/signup", dependencies=[Depends(limit("signup", 5, 3600))])
async def signup(
    body: SignupIn, response: Response, session: Session, settings: Annotated[Settings, Depends(get_settings)]
) -> dict:
    if not settings.signup_enabled:
        raise HTTPException(403, "Sign-ups are closed. Ask an owner for an account.")
    email = body.email.strip().lower()
    if not EMAIL.match(email):
        raise HTTPException(422, "Enter a valid email address")
    if len(body.password) < passwords.MIN_LENGTH:
        raise HTTPException(422, f"Use at least {passwords.MIN_LENGTH} characters for the password")
    if await _by_email(session, email) is not None:
        raise HTTPException(409, "An account with this email already exists. Sign in instead.")
    role = Role.OWNER if settings.signup_role == Role.OWNER else Role.VIEWER
    user = User(
        shop_id=settings.shop_id,
        email=email,
        name=body.name.strip(),
        password_hash=passwords.hash_password(body.password),
        role=role,
    )
    session.add(user)
    await session.flush()
    audit(session, "system", "member.joined", user=user.id, role=role)
    out = _start_session(response, settings, user)
    await session.commit()
    return out


@router.get("/options")
def options(settings: Annotated[Settings, Depends(get_settings)]) -> dict:
    """Public: what the login and signup pages show (the demo account's email, whether sign-ups are open)."""
    return {
        "demo_email": settings.demo_email,
        "signup_enabled": settings.signup_enabled,
        "signup_role": settings.signup_role,
    }


@router.post("/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@router.get("/me")
def me(user: Member) -> dict:
    return {"shop_id": user.shop_id, **member_out(user)}


@router.post("/password")
async def change_password(body: PasswordIn, user: Member, session: Session) -> dict:
    if user.is_demo:
        raise HTTPException(403, "The shared demo account's password is set on the server (ADMIN_PASSWORD).")
    if not passwords.verify(body.current, user.password_hash):
        raise HTTPException(400, "Your current password is wrong")
    if len(body.new) < passwords.MIN_LENGTH:
        raise HTTPException(422, f"Use at least {passwords.MIN_LENGTH} characters for the password")
    user.password_hash = passwords.hash_password(body.new)
    audit(session, "owner", "member.password_changed")
    await session.commit()
    return {"ok": True}


@router.get("/team")
async def team(user: Member, session: Session) -> list[dict]:
    rows = await session.execute(select(User).where(User.shop_id == user.shop_id).order_by(User.created_at))
    return [member_out(u) for u in rows.scalars()]


async def _member(session: AsyncSession, shop_id: str, member_id: str) -> User:
    member = await session.get(User, member_id)
    if member is None or member.shop_id != shop_id:
        raise HTTPException(404, "No such member")
    if member.is_demo:
        raise HTTPException(403, "The shared demo account always stays an owner")
    return member


async def _other_owners(session: AsyncSession, shop_id: str, member_id: str) -> int:
    return (
        await session.scalar(
            select(func.count()).where(User.shop_id == shop_id, User.role == Role.OWNER, User.id != member_id)
        )
    ) or 0


@router.patch("/team/{member_id}")
async def set_role(member_id: str, body: RoleIn, shop_id: ManagerShop, session: Session) -> dict:
    member = await _member(session, shop_id, member_id)
    if body.role == Role.VIEWER and not await _other_owners(session, shop_id, member.id):
        raise HTTPException(409, "A shop needs at least one owner")
    member.role = body.role
    audit(session, "owner", "member.role_changed", member=member.id, role=body.role)
    await session.commit()
    return member_out(member)


@router.delete("/team/{member_id}")
async def remove(member_id: str, shop_id: ManagerShop, user: Member, session: Session) -> dict:
    if member_id == user.id:
        raise HTTPException(409, "You can't remove yourself")
    member = await _member(session, shop_id, member_id)
    await session.delete(member)
    audit(session, "owner", "member.removed", member=member_id)
    await session.commit()
    return {"ok": True}
