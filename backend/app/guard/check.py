"""The guard check: runs before every PayPal order is created. Hot path, so it reads a small cached
copy of the guard state, makes one indexed count per check (device velocity, on ix_attempts_shop_fp_ts)
and one more indexed lookup only for a held SKU.

Decision order (first match wins):
  1. kill switch on            → allow (owner turned the guard off)
  2. fingerprint blocked       → block (unexpired entries in device_entries, app/guard/devices.py)
  3. device velocity: this device made 6+ attempts or had 2+ declines in 10 min → challenge
     (a valid challenge token lets it through up to 12 attempts / 4 declines; past that → block until its
     attempts age out of the window, since another challenge could not let it through anyway)
  4. network velocity: 6+ devices with 3+ declines from this device's network (IPv4 /24) in 10 min, and no
     valid challenge token → challenge (a bot that rotates its device id on every request)
  5. SKU on hold + first-time fingerprint without a valid challenge token → challenge
  6. SCA mode + card payment + first-time fingerprint → allow_with_sca (3-D Secure SCA_ALWAYS). A returning
     customer is not asked: targeted 3-D Secure costs real customers no friction (reason returning_customer)
  7. otherwise                 → allow

Payment hold: an allowed order from a first-time fingerprint is held, created with intent AUTHORIZE, so the
bank approves it but nothing settles. In normal times that is the owner's review window (review_hold_min,
10 min by default: the payment is captured after it); while an incident's fraud defences are on it lasts
until the incident is judged. app/actions/holds.py voids the holds an attack matches (no fee, nothing to
dispute) and captures the rest. Because the window starts at checkout, the attack's first payments, made
before anything was detected, are still holds when the attack is confirmed: voided, not refunded. A
returning customer's order is captured at once.

"First-time" means no capture from this device before the attack window (KNOWN_BEFORE ahead of the moment
the defences went on): a bot whose test payments got through before detection is not a returning customer.

The velocity breaker works with no incident open and no AI: it slows one device down within seconds,
while detection needs a burst across the shop. It keys on the device: a signed cookie the shop server
issued (app/api/shop.py), or the client's own device id when it sends no cookie. A bot that drops cookies
and rotates ids escapes it, so the network rule (4) counts devices per network instead (app/guard/signals.py:
the client IP counted from the right of X-Forwarded-For, so it cannot be typed in). A network is shared by
everyone behind one carrier NAT or office, so that rule only challenges (a person passes the human check),
and only when the network also brings several declines.

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
from datetime import UTC, datetime, timedelta

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
# A returning customer captured before the attack began: same lead the detector uses for incident windows.
KNOWN_BEFORE = timedelta(minutes=15)
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


@dataclass(frozen=True)
class NetworkLimits:
    window: timedelta = timedelta(minutes=10)
    devices: int = 6
    declines: int = 3


NETWORK_LIMITS = NetworkLimits()


@dataclass
class GuardContext:
    shop_id: str
    sku: str
    method: str
    fingerprint_hash: str
    challenge_token: str | None = None
    ip_net_hash: str | None = None  # app/guard/signals.py; None for callers that know no client IP


@dataclass
class GuardVerdict:
    decision: Decision
    reason: str
    incident_id: str | None = None
    hold: bool = False  # authorise, don't capture (payment hold); only with an allow decision
    hold_reason: str | None = None  # "review" (first-time shopper, normal times) or "incident"


@dataclass
class _CachedState:
    mode: str
    blocked: frozenset[str]
    held_skus: frozenset[str]
    kill_switch: bool
    incident_id: str | None
    updated_at: datetime
    review_hold_min: int
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
        review_hold_min=ms.review_hold_min if ms else 0,
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


async def _network_activity(
    session: AsyncSession, shop_id: str, net: str, window: timedelta
) -> tuple[int, int]:
    """(distinct devices, declines) from this network in the window, before this attempt."""
    q = select(
        func.count(func.distinct(CheckoutAttempt.fingerprint_hash)),
        func.count().filter(CheckoutAttempt.outcome == Outcome.DECLINED),
    ).where(
        CheckoutAttempt.shop_id == shop_id,
        CheckoutAttempt.ip_net_hash == net,
        CheckoutAttempt.ts > func.now() - window,
    )
    devices_seen, declines = (await session.execute(q)).one()
    return devices_seen, declines


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
    if ctx.ip_net_hash and not has_token:
        lim = NETWORK_LIMITS
        net_devices, net_declines = await _network_activity(session, ctx.shop_id, ctx.ip_net_hash, lim.window)
        if net_devices >= lim.devices and net_declines >= lim.declines:
            return GuardVerdict(Decision.CHALLENGE, "network_velocity", st.incident_id)
    defences_on = st.incident_id is not None and (st.mode == GuardMode.SCA_ALL or bool(st.held_skus))
    known: bool | None = None
    if defences_on or st.review_hold_min > 0:
        # Before the attack window when defences are on; otherwise before KNOWN_BEFORE ago (a device that
        # paid minutes ago in this very burst is not "returning").
        since = (st.updated_at if defences_on else datetime.now(UTC)) - KNOWN_BEFORE
        known = await _is_known_customer(session, ctx.shop_id, ctx.fingerprint_hash, since)
    if ctx.sku in st.held_skus and not has_token and not known:
        return GuardVerdict(Decision.CHALLENGE, "sku_on_hold", st.incident_id)
    reason = "incident" if defences_on else "review" if st.review_hold_min > 0 else None
    hold = reason is not None and not known
    held = {"hold": hold, "hold_reason": reason if hold else None}
    if st.mode == GuardMode.SCA_ALL and ctx.method == Method.CARD:
        if not known:
            return GuardVerdict(Decision.ALLOW_WITH_SCA, "sca_mode", st.incident_id, **held)
        return GuardVerdict(Decision.ALLOW, "returning_customer", st.incident_id)
    if hold and defences_on:
        return GuardVerdict(Decision.ALLOW, "payment_hold", st.incident_id, **held)
    return GuardVerdict(Decision.ALLOW, "normal", **held)


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
