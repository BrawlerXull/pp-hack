"""The guard check: runs before every PayPal order is created. Hot path, so it reads a small cached
copy of the guard state and makes at most one indexed query.

Decision order (first match wins):
  1. kill switch on            → allow (owner turned the guard off)
  2. fingerprint blocked       → block
  3. SKU on hold + first-time fingerprint without a valid challenge token → challenge
  4. SCA mode + card payment   → allow_with_sca (order is created with 3-D Secure SCA_ALWAYS)
  5. otherwise                 → allow
"""

import time
from dataclasses import dataclass, field
from datetime import datetime

from itsdangerous import BadSignature, TimestampSigner
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import CheckoutAttempt, Decision, GuardMode, GuardState, MerchantSettings, Method, Outcome

CACHE_TTL_S = 2.0
CHALLENGE_MAX_AGE_S = 600


@dataclass
class GuardContext:
    shop_id: str
    sku: str
    method: str
    fingerprint_hash: str
    challenge_token: str | None = None


@dataclass
class GuardVerdict:
    decision: Decision
    reason: str
    incident_id: str | None = None


@dataclass
class _CachedState:
    mode: str
    blocked: frozenset[str]
    held_skus: frozenset[str]
    kill_switch: bool
    incident_id: str | None
    updated_at: datetime
    loaded_at: float = field(default_factory=time.monotonic)


_cache: dict[str, _CachedState] = {}


def invalidate_cache(shop_id: str | None = None) -> None:
    if shop_id is None:
        _cache.clear()
    else:
        _cache.pop(shop_id, None)


async def _state(session: AsyncSession, shop_id: str) -> _CachedState:
    cached = _cache.get(shop_id)
    if cached and time.monotonic() - cached.loaded_at < CACHE_TTL_S:
        return cached
    gs = await session.get(GuardState, shop_id, populate_existing=True)
    ms = await session.get(MerchantSettings, shop_id, populate_existing=True)
    state = _CachedState(
        mode=gs.mode if gs else GuardMode.NORMAL,
        blocked=frozenset(gs.blocked_fingerprints or []) if gs else frozenset(),
        held_skus=frozenset(gs.held_skus or []) if gs else frozenset(),
        kill_switch=bool(ms and ms.kill_switch),
        incident_id=gs.set_by_incident if gs else None,
        updated_at=gs.updated_at if gs else datetime.min,
    )
    _cache[shop_id] = state
    return state


def _signer() -> TimestampSigner:
    return TimestampSigner(get_settings().session_secret, salt="cg-challenge")


def issue_challenge_token(fingerprint_hash: str) -> str:
    return _signer().sign(fingerprint_hash).decode()


def challenge_token_valid(token: str | None, fingerprint_hash: str) -> bool:
    if not token:
        return False
    try:
        return _signer().unsign(token, max_age=CHALLENGE_MAX_AGE_S).decode() == fingerprint_hash
    except BadSignature:
        return False


async def _is_known_customer(session: AsyncSession, shop_id: str, fp: str, before: datetime) -> bool:
    q = select(
        exists().where(
            CheckoutAttempt.shop_id == shop_id,
            CheckoutAttempt.fingerprint_hash == fp,
            CheckoutAttempt.outcome == Outcome.CAPTURED,
            CheckoutAttempt.ts < before,
        )
    )
    return bool((await session.execute(q)).scalar())


async def check(session: AsyncSession, ctx: GuardContext) -> GuardVerdict:
    st = await _state(session, ctx.shop_id)
    if st.kill_switch:
        return GuardVerdict(Decision.ALLOW, "guard_disabled")
    if ctx.fingerprint_hash in st.blocked:
        return GuardVerdict(Decision.BLOCK, "fingerprint_blocked", st.incident_id)
    if ctx.sku in st.held_skus and not challenge_token_valid(ctx.challenge_token, ctx.fingerprint_hash):
        if not await _is_known_customer(session, ctx.shop_id, ctx.fingerprint_hash, st.updated_at):
            return GuardVerdict(Decision.CHALLENGE, "sku_on_hold", st.incident_id)
    if st.mode == GuardMode.SCA_ALL and ctx.method == Method.CARD:
        return GuardVerdict(Decision.ALLOW_WITH_SCA, "sca_mode", st.incident_id)
    return GuardVerdict(Decision.ALLOW, "normal")
