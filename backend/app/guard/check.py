"""The guard check: runs before every PayPal order is created. Hot path, so it reads a small cached
copy of the guard state, makes one indexed count per check (device velocity, on ix_attempts_shop_fp_ts)
and one more indexed lookup only for a held SKU.

Decision order (first match wins):
  1. kill switch on            → allow (owner turned the guard off)
  2. fingerprint blocked       → block (unexpired entries in device_entries, app/guard/devices.py)
  3. device velocity: this device made 6+ attempts or had 2+ declines in 10 min → challenge
     (a valid challenge token lets it through up to 12 attempts / 4 declines; past that → block until its
     attempts age out of the window, since another challenge could not let it through anyway)
  4. SKU on hold + first-time fingerprint without a valid challenge token → challenge
  5. SCA mode + card payment   → allow_with_sca (order is created with 3-D Secure SCA_ALWAYS)
  6. otherwise                 → allow

The velocity breaker works with no incident open and no AI: it slows one device down within seconds,
while detection needs a burst across the shop. It keys on the device fingerprint, never on the IP: the IP
comes from X-Forwarded-For (spoofable) and is shared by every shopper behind one carrier NAT. The
fingerprint comes from a client-supplied device id, so a bot that rotates ids escapes this check; the
shop-wide triggers (cheap_sku_card_cycling, decline_spike) are what catch that.

Parallel requests: place_order takes lock_device() before check() and commits the attempt row (outcome
CREATED) before calling PayPal, so N parallel requests from one device are counted one after another and
at most `attempts` of them reach PayPal. Declines are only known once PayPal answers, so the decline limit
only slows a device down from its next request on.

Fail-open: check_within_budget() gives the check GUARD_CHECK_BUDGET_MS. If it runs over (lock contention,
a slow or failing database), the checkout goes on with the last guard state this process saw (at most
10 min old): blocked devices stay blocked and 3-D Secure stays on, but the velocity count and the SKU hold
are skipped. With no such state the payment is allowed, recorded as guard_unavailable. A slow guard must
never take the shop's checkout down with it.

The challenge is a demo click-through ("I'm a real shopper"): /shop/challenge issues a token with no real
proof. The token tier is a convenience for people, not a bot barrier; a production shop would issue it only
after a server-verified captcha.
"""

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import structlog
from itsdangerous import BadSignature, TimestampSigner
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import CheckoutAttempt, Decision, GuardMode, GuardState, MerchantSettings, Method, Outcome
from app.guard import devices

log = structlog.get_logger()

CACHE_TTL_S = 2.0
STALE_STATE_MAX_S = 600.0  # fail-open uses the last state seen if it is at most this old
CHALLENGE_MAX_AGE_S = 600


@dataclass(frozen=True)
class DeviceLimits:
    window: timedelta = timedelta(minutes=10)
    attempts: int = 6
    declines: int = 2
    # With a valid challenge token: a person who clicked through the challenge still retries a card or
    # two, but a token (free to get in this demo) must not buy a bot unlimited tries.
    attempts_with_token: int = 12
    declines_with_token: int = 4


DEVICE_LIMITS = DeviceLimits()


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
        # Expired blocks are filtered here too, not only pruned by the worker (which may be stopped).
        blocked=await devices.blocked(session, shop_id),
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


async def lock_device(session: AsyncSession, shop_id: str, fp: str) -> None:
    """Serialise check-then-insert per device until the caller commits (two-key advisory lock: its own
    key space, apart from the worker's single-key lock)."""
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(shop_id), func.hashtext(fp))))


async def _device_activity(
    session: AsyncSession, shop_id: str, fp: str, window: timedelta
) -> tuple[int, int]:
    """(attempts, declines) by this device in the window, before this one. Guard-stopped attempts count:
    a bot that keeps hammering after a challenge stays challenged."""
    q = select(func.count(), func.count().filter(CheckoutAttempt.outcome == Outcome.DECLINED)).where(
        CheckoutAttempt.shop_id == shop_id,
        CheckoutAttempt.fingerprint_hash == fp,
        CheckoutAttempt.ts > func.now() - window,
    )
    attempts, declines = (await session.execute(q)).one()
    return attempts, declines


def device_over_limit(
    attempts: int, declines: int, has_token: bool, lim: DeviceLimits = DEVICE_LIMITS
) -> bool:
    if has_token:
        return attempts >= lim.attempts_with_token or declines >= lim.declines_with_token
    return attempts >= lim.attempts or declines >= lim.declines


def device_past_token_limit(attempts: int, declines: int, lim: DeviceLimits = DEVICE_LIMITS) -> bool:
    """Even a challenge token would not let this device through: block instead of offering a challenge
    that cannot succeed (the shopper would loop on it)."""
    return attempts >= lim.attempts_with_token or declines >= lim.declines_with_token


async def check(session: AsyncSession, ctx: GuardContext) -> GuardVerdict:
    st = await _state(session, ctx.shop_id)
    if st.kill_switch:
        return GuardVerdict(Decision.ALLOW, "guard_disabled")
    if ctx.fingerprint_hash in st.blocked:
        return GuardVerdict(Decision.BLOCK, "fingerprint_blocked", st.incident_id)
    has_token = challenge_token_valid(ctx.challenge_token, ctx.fingerprint_hash)
    attempts, declines = await _device_activity(
        session, ctx.shop_id, ctx.fingerprint_hash, DEVICE_LIMITS.window
    )
    if device_past_token_limit(attempts, declines):
        return GuardVerdict(Decision.BLOCK, "device_velocity_limit", st.incident_id)
    if device_over_limit(attempts, declines, has_token):
        return GuardVerdict(Decision.CHALLENGE, "device_velocity", st.incident_id)
    if ctx.sku in st.held_skus and not has_token:
        if not await _is_known_customer(session, ctx.shop_id, ctx.fingerprint_hash, st.updated_at):
            return GuardVerdict(Decision.CHALLENGE, "sku_on_hold", st.incident_id)
    if st.mode == GuardMode.SCA_ALL and ctx.method == Method.CARD:
        return GuardVerdict(Decision.ALLOW_WITH_SCA, "sca_mode", st.incident_id)
    return GuardVerdict(Decision.ALLOW, "normal")


def fail_open_verdict(ctx: GuardContext) -> GuardVerdict:
    """The check could not finish in time: keep the defences this process last saw, let the rest through."""
    st = _cache.get(ctx.shop_id)
    if st is None or time.monotonic() - st.loaded_at > STALE_STATE_MAX_S:
        return GuardVerdict(Decision.ALLOW, "guard_unavailable")
    if st.kill_switch:
        return GuardVerdict(Decision.ALLOW, "guard_disabled")
    if ctx.fingerprint_hash in st.blocked:
        return GuardVerdict(Decision.BLOCK, "fingerprint_blocked", st.incident_id)
    if st.mode == GuardMode.SCA_ALL and ctx.method == Method.CARD:
        return GuardVerdict(Decision.ALLOW_WITH_SCA, "sca_mode", st.incident_id)
    return GuardVerdict(Decision.ALLOW, "guard_unavailable")


async def check_within_budget(session: AsyncSession, ctx: GuardContext, *, lock: bool) -> GuardVerdict:
    """check() (after lock_device() when `lock`) within GUARD_CHECK_BUDGET_MS, else fail_open_verdict().
    On a timeout or database error the session is rolled back, so the caller can still record the attempt;
    the device lock is gone with it, so parallel requests from one device are not serialised then."""
    budget = get_settings().guard_check_budget_ms / 1000
    started = time.monotonic()

    async def guarded() -> GuardVerdict:
        if lock:
            await lock_device(session, ctx.shop_id, ctx.fingerprint_hash)
        return await check(session, ctx)

    try:
        return await asyncio.wait_for(guarded(), timeout=budget)
    except Exception as exc:  # timeout or the database failing: the checkout must go on without the guard
        verdict = fail_open_verdict(ctx)
        log.warning(
            "guard_check_failed_open",
            shop=ctx.shop_id,
            error=type(exc).__name__,
            elapsed_ms=round((time.monotonic() - started) * 1000),
            decision=verdict.decision,
            reason=verdict.reason,
        )
        try:
            await session.rollback()
        except Exception:  # the connection may be gone too; the caller's next statement gets a fresh one
            log.warning("guard_check_rollback_failed", shop=ctx.shop_id)
        return verdict
