import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from app.api.deps import SESSION_COOKIE, SESSION_MAX_AGE_S, OwnerShop, issue_session
from app.config import Settings, get_settings

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginIn(BaseModel):
    password: str


@router.post("/login")
def login(body: LoginIn, response: Response, settings: Annotated[Settings, Depends(get_settings)]) -> dict:
    if not hmac.compare_digest(body.password, settings.admin_password):
        raise HTTPException(401, "wrong password")
    token = issue_session(settings)
    response.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_MAX_AGE_S, httponly=True, samesite="lax", secure=False
    )
    return {"ok": True, "shop_id": settings.shop_id, "token": token}


@router.post("/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@router.get("/me")
def me(shop_id: OwnerShop) -> dict:
    return {"shop_id": shop_id, "role": "owner"}
