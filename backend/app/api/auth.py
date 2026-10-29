import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from app.api import ratelimit
from app.api.deps import SESSION_COOKIE, SESSION_MAX_AGE_S, OwnerShop, issue_session
from app.api.ratelimit import limit
from app.config import Settings, get_settings

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginIn(BaseModel):
    password: str


# Password guessing: after 10 wrong passwords from one client in 5 minutes, every login from it waits (the
# right password too, or a guesser would learn it from the different answer). Only failures count, so
# judges logging in again and again never use up the allowance.
@router.post("/login", dependencies=[Depends(limit("login", 10, 300, count_all=False))])
def login(
    body: LoginIn, request: Request, response: Response, settings: Annotated[Settings, Depends(get_settings)]
) -> dict:
    if not hmac.compare_digest(body.password, settings.admin_password):
        ratelimit.record("login", request)
        raise HTTPException(401, "wrong password")
    token = issue_session(settings)
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
    return {"ok": True, "shop_id": settings.shop_id, "token": token}


@router.post("/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@router.get("/me")
def me(shop_id: OwnerShop) -> dict:
    return {"shop_id": shop_id, "role": "owner"}
