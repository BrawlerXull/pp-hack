"""Connecting a shop's own checkout (the Developers page): API keys, whether calls are arriving, and the
public OpenAPI spec of the guard API that the SDKs are generated from (sdks/, with APIMatic).

Any member can see the keys (never the secret) and the connection status; only owners create or revoke keys.
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import apikeys, visitor
from app.api.deps import ManagerShop, OwnerShop
from app.config import get_settings
from app.db.audit import audit
from app.db.models import ApiKey, CheckoutAttempt, PayPalEvent, Source
from app.db.session import get_session

router = APIRouter(prefix="/integration", tags=["integration"])
spec_router = APIRouter(prefix="/v1", tags=["guard"])
Session = Annotated[AsyncSession, Depends(get_session)]

MAX_KEYS = 20


class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


def _key_out(k: ApiKey) -> dict[str, Any]:
    return {
        "id": k.id,
        "name": k.name,
        "prefix": k.prefix,
        "created_at": k.created_at,
        "last_used_at": k.last_used_at,
    }


@router.get("/keys")
async def list_keys(shop_id: OwnerShop, session: Session) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(ApiKey)
            .where(ApiKey.shop_id == shop_id, ApiKey.revoked_at.is_(None))
            .order_by(ApiKey.created_at.desc())
        )
    ).scalars()
    return [_key_out(k) for k in rows]


@router.post("/keys")
async def create_key(body: KeyIn, shop_id: ManagerShop, session: Session) -> dict[str, Any]:
    """The key itself is in this answer only: it is stored as a hash."""
    live = (
        await session.execute(
            select(func.count()).where(ApiKey.shop_id == shop_id, ApiKey.revoked_at.is_(None))
        )
    ).scalar_one()
    if live >= MAX_KEYS:
        raise HTTPException(409, f"A shop can have {MAX_KEYS} keys. Revoke one you no longer use.")
    secret = apikeys.generate()
    key = ApiKey(
        shop_id=shop_id,
        name=body.name.strip(),
        prefix=secret[: apikeys.SHOWN],
        key_hash=apikeys.digest(secret),
        created_by=visitor.current(),
    )
    session.add(key)
    await session.flush()
    audit(session, "owner", "api_key.created", key_id=key.id, name=key.name)
    await session.commit()
    await session.refresh(key)
    return _key_out(key) | {"key": secret}


@router.delete("/keys/{key_id}")
async def revoke_key(key_id: str, shop_id: ManagerShop, session: Session) -> dict[str, Any]:
    key = await session.get(ApiKey, key_id)
    if key is None or key.shop_id != shop_id or key.revoked_at is not None:
        raise HTTPException(404, "key not found")
    key.revoked_at = datetime.now(UTC)
    audit(session, "owner", "api_key.revoked", key_id=key.id, name=key.name)
    await session.commit()
    return {"ok": True}


@router.get("/status")
async def status(shop_id: OwnerShop, session: Session) -> dict[str, Any]:
    """Is the shop's checkout calling us? The newest key use, the guard checks recorded through the API in the
    last 24 h and the latest one, and the latest verified PayPal webhook."""
    since = datetime.now(UTC) - timedelta(hours=24)
    last_call = (
        await session.execute(select(func.max(ApiKey.last_used_at)).where(ApiKey.shop_id == shop_id))
    ).scalar()
    api = (CheckoutAttempt.shop_id == shop_id, CheckoutAttempt.source == Source.API)
    checks = (
        await session.execute(select(func.count()).where(*api, CheckoutAttempt.ts > since))
    ).scalar_one()
    latest = (
        await session.execute(
            select(CheckoutAttempt).where(*api).order_by(CheckoutAttempt.ts.desc()).limit(1)
        )
    ).scalar_one_or_none()
    webhook = (
        await session.execute(
            select(func.max(PayPalEvent.received_at)).where(PayPalEvent.verified == "SUCCESS")
        )
    ).scalar()
    return {
        "last_call_at": last_call,
        "checks_24h": checks,
        "latest_check": {
            "ts": latest.ts,
            "sku": latest.sku,
            "decision": latest.guard_decision,
            "outcome": latest.outcome,
        }
        if latest
        else None,
        "last_webhook_at": webhook,
        "webhook_path": "/webhooks/paypal",
    }


def public_spec(request: Request) -> dict[str, Any]:
    """The app's OpenAPI document cut down to the public guard API, with the deployed URL as its server."""
    full = request.app.openapi()
    paths = {p: v for p, v in full["paths"].items() if p.startswith("/v1/guard/")}
    schemas = full.get("components", {}).get("schemas", {})
    used: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                name = ref.rsplit("/", 1)[1]
                if name not in used:
                    used.add(name)
                    walk(schemas.get(name))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(paths)
    kept = {n: _untitled(schemas[n]) for n in sorted(used)}
    return {
        "openapi": full["openapi"],
        "info": {
            "title": "Checkout Guard API",
            "version": "1.0.0",
            "description": "Ask Checkout Guard before you create a PayPal order, and tell it what happened. "
            "Get a key on the Connect store page of your Checkout Guard dashboard.",
            "contact": {"name": "Checkout Guard", "url": "https://github.com/BrawlerXull/pp-hack"},
            "license": {"name": "MIT", "url": "https://opensource.org/license/mit"},
        },
        "servers": [
            {"url": get_settings().public_app_url.rstrip("/") + "/api", "description": "This deployment"}
        ],
        "paths": paths,
        "components": {
            "schemas": kept,
            "securitySchemes": full["components"]["securitySchemes"],
        },
    }


def _untitled(schema: Any) -> Any:
    """The schema without the `title` Pydantic puts on every property: generators read two properties with
    the same title in different models (attempt_id, outcome) as a clash. Model titles stay."""
    if isinstance(schema, dict):
        out = {k: _untitled(v) for k, v in schema.items() if k != "properties"}
        if "properties" in schema:
            out["properties"] = {
                name: {k: _untitled(v) for k, v in prop.items() if k != "title"}
                for name, prop in schema["properties"].items()
            }
        return out
    if isinstance(schema, list):
        return [_untitled(v) for v in schema]
    return schema


@spec_router.get("/openapi.json", include_in_schema=False)
def openapi_spec(request: Request) -> dict[str, Any]:
    return public_spec(request)
