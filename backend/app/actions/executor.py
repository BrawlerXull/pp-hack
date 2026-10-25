"""Applies policy-approved actions: guard-state changes and capped refunds through PayPal."""

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import Select, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.audit import audit
from app.db.models import (
    Action,
    ActionStatus,
    ActionType,
    CheckoutAttempt,
    Decision,
    GuardMode,
    GuardState,
    Incident,
    IncidentStatus,
    MerchantSettings,
    Outcome,
)
from app.events import publish
from app.guard import devices
from app.guard.check import invalidate_cache
from app.paypal.client import PayPalClient, PayPalResponse
from app.paypal.orders import refund_capture, refund_request_id
from app.policy.engine import PlannedAction

log = structlog.get_logger()
REFUND_LOOKBACK = timedelta(minutes=15)  # captures shortly before the incident opened still count
REFUND_NOTE = "Refunded by the merchant: this payment was flagged as unauthorised card testing."
MAX_REFUND_FAILURES = 3  # definitive PayPal refusals before a capture stops being re-tried
SWEEP_AFTER = timedelta(minutes=5)  # well past the client's worst case (30 s timeout, 3 retries)
MAX_SWEEPS = 6

DISPUTE_DOUBLE_LOSS = (
    "This payment is disputed. If the buyer's bank or PayPal returns the money through the dispute, a refund "
    "now pays them a second time and the chargeback fee still applies. Respond to the dispute in PayPal "
    "instead, or approve again with the override if a refund is how you are settling it."
)


class DisputedCapture(Exception):
    def __init__(self, dispute_status: str | None):
        super().__init__(DISPUTE_DOUBLE_LOSS)
        self.dispute_status = dispute_status


@dataclass(frozen=True)
class Match:
    attempt_id: str
    strong: bool
    matched_on: list[str]


def classify_match(
    fingerprint: str, email_domain: str | None, sig: dict[str, Any], blocked: Collection[str]
) -> tuple[bool, list[str]] | None:
    """Why a capture on the attacked item matches the attack, and whether that is STRONG (refundable
    without the owner): its device is a strong fingerprint (several cards/payers or guessing refusals) or
    is in `blocked`, the devices another incident blocked as strong (devices.blocked_as_strong). Any other
    match is WEAK: a flagged device (even one this incident blocked), one honest-looking decline, or only
    a suspect email domain (a real first-time shopper can share one). None: no reason to touch it."""
    on = []
    if fingerprint in sig.get("strong_fingerprints", []):
        on.append("strong_fingerprint")
    if fingerprint in blocked:
        on.append("blocked_device")
    strong = bool(on)
    if not strong and fingerprint in sig.get("fingerprints", []):
        on.append("flagged_device")
    if fingerprint in sig.get("weak_fingerprints", []):
        on.append("single_decline_device")
    if email_domain and email_domain in sig.get("suspect_domains", []):
        on.append("suspect_domain")
    return (strong, on) if on else None


def snapshot(gs: GuardState) -> dict[str, Any]:
    # Device blocks are not part of the snapshot: they outlive the incident (and expire on their own).
    return {"mode": gs.mode, "held_skus": list(gs.held_skus or [])}


class Executor:
    def __init__(self, session: AsyncSession, paypal: PayPalClient | None):
        self.s = session
        self.paypal = paypal

    async def _record(
        self, incident: Incident, type_: ActionType, rule: str, status: ActionStatus, **kw: Any
    ) -> Action:
        action = Action(incident_id=incident.id, type=type_, policy_rule=rule, status=status, **kw)
        self.s.add(action)
        await self.s.flush()
        audit(
            self.s,
            "executor",
            f"action.{status}",
            incident.id,
            type=type_,
            rule=rule,
            params=kw.get("params"),
        )
        await publish(self.s, "action", action_event(action))
        return action

    async def apply(self, incident: Incident, planned: list[PlannedAction]) -> list[Action]:
        """Commits. Guard-state changes go first and are committed at once, so 3-D Secure, blocks and holds
        are live (and the GuardState row lock released: owner reset, close) before any PayPal refund call.
        Refunds then run one short transaction per capture."""
        done = await self._apply_guard(incident, [p for p in planned if p.type != ActionType.REFUND])
        await self.s.commit()
        invalidate_cache(incident.shop_id)
        for p in planned:
            if p.type == ActionType.REFUND:
                done.extend(await self.refund_matched(incident, p))
        return done

    async def _apply_guard(self, incident: Incident, planned: list[PlannedAction]) -> list[Action]:
        gs = await self.s.get(GuardState, incident.shop_id, with_for_update=True, populate_existing=True)
        assert gs is not None
        if incident.previous_guard_state is None:
            incident.previous_guard_state = snapshot(gs)
        done: list[Action] = []

        for p in planned:
            if p.type == ActionType.SCA_ON and gs.mode != GuardMode.SCA_ALL:
                gs.mode = GuardMode.SCA_ALL
                done.append(await self._record(incident, p.type, p.rule, ActionStatus.EXECUTED))
            elif p.type == ActionType.BLOCK_FP:
                flagged = (incident.signature or {}).get("flag_reasons", {})
                reasons = {
                    fp: f"{p.rule}: {flagged[fp]}" if fp in flagged else p.rule
                    for fp in p.params["fingerprints"]
                }
                new = await devices.block(
                    self.s,
                    incident.shop_id,
                    p.params["fingerprints"],
                    incident.id,
                    reasons,
                    datetime.now(UTC),
                )
                if new:
                    done.append(
                        await self._record(
                            incident, p.type, p.rule, ActionStatus.EXECUTED, params={"fingerprints": new}
                        )
                    )
            elif p.type == ActionType.RELAX:
                if action := await self._relax(incident, gs, p):
                    done.append(action)
            elif p.type == ActionType.HOLD_SKU and p.params["sku"] not in (gs.held_skus or []):
                gs.held_skus = sorted({*(gs.held_skus or []), p.params["sku"]})
                done.append(
                    await self._record(incident, p.type, p.rule, ActionStatus.EXECUTED, params=p.params)
                )

        gs.set_by_incident = incident.id
        gs.updated_at = datetime.now(UTC)
        return done

    async def _relax(self, incident: Incident, gs: GuardState, p: PlannedAction) -> Action | None:
        """Restore the 3-D Secure mode and SKU holds from before this incident. Only when this incident set
        the current state (another incident's defences are not ours to undo); fingerprint blocks stay, since
        a device that was blocked was blocked for what it did. Idempotent: nothing to restore, no action."""
        prev = incident.previous_guard_state
        if prev is None or gs.set_by_incident != incident.id:
            return None
        released = sorted(set(gs.held_skus or []) - set(prev["held_skus"]))
        if gs.mode == prev["mode"] and not released:
            return None
        gs.mode = prev["mode"]
        gs.held_skus = sorted(set(gs.held_skus or []) & set(prev["held_skus"]))
        return await self._record(
            incident,
            p.type,
            p.rule,
            ActionStatus.EXECUTED,
            params={"restored_mode": prev["mode"], "released_skus": released},
        )

    @staticmethod
    def _handled(shop_id: str, attempt_id: str | None = None) -> Select[tuple[str | None]]:
        """Attempt ids this shop has settled a refund decision for, in ANY incident: refunded, refunding,
        awaiting the owner, or kept by the owner ("Keep" = rejected). A failed refund may be retried, up to
        MAX_REFUND_FAILURES, so a capture PayPal will never refund is not re-sent every tick."""
        aid = Action.params["attempt_id"].as_string()
        q = (
            select(aid)
            .join(Incident, Incident.id == Action.incident_id)
            .where(Incident.shop_id == shop_id, Action.type == ActionType.REFUND, aid.is_not(None))
        )
        if attempt_id is not None:
            q = q.where(aid == attempt_id)
        return q.group_by(aid).having(
            or_(func.bool_or(Action.status != ActionStatus.FAILED), func.count() >= MAX_REFUND_FAILURES)
        )

    async def _matches(self, incident: Incident) -> list[Match]:
        """Candidates only, unlocked: each one is locked and re-checked in its own transaction. Never a
        disputed capture (a refund on top of a chargeback pays the buyer twice), never a device the owner
        trusts, never one already handled anywhere in the shop."""
        sig = incident.signature or {}
        top_sku = sig.get("top_sku")
        now = datetime.now(UTC)
        trusted = await devices.trusted(self.s, incident.shop_id, now)
        # No SKU filter in SQL: a STRONG device (several cards or fraud refusals, never a known customer) is a
        # bot whatever it bought, so its captures on other items, or in an attack with no single target
        # item, are refunded too. Weaker matches stay limited to the attacked item (below), where a real
        # first-time shopper sharing an email domain or one honest refusal is most likely to be.
        q = (
            select(
                CheckoutAttempt.id,
                CheckoutAttempt.fingerprint_hash,
                CheckoutAttempt.email_domain,
                CheckoutAttempt.sku,
            )
            .where(
                CheckoutAttempt.shop_id == incident.shop_id,
                CheckoutAttempt.outcome == Outcome.CAPTURED,
                CheckoutAttempt.capture_id.is_not(None),
                CheckoutAttempt.disputed.is_(False),
                CheckoutAttempt.ts >= incident.opened_at - REFUND_LOOKBACK,
                CheckoutAttempt.id.not_in(self._handled(incident.shop_id)),
            )
            .order_by(CheckoutAttempt.ts)
        )
        if trusted:
            q = q.where(CheckoutAttempt.fingerprint_hash.not_in(trusted))
        blocked = await devices.blocked_as_strong(self.s, incident.shop_id, now, incident.id)
        out = []
        for attempt_id, fp, domain, sku in (await self.s.execute(q)).all():
            m = classify_match(fp, domain, sig, blocked)
            if m is None:
                continue
            strong, matched_on = m
            if not strong and (top_sku is None or sku != top_sku):
                continue
            if strong and sku != top_sku:
                matched_on = [*matched_on, "other_item"]
            out.append(Match(attempt_id, strong, matched_on))
        return out

    async def _refunded_total(self, incident: Incident) -> Decimal:
        """Executed and in-flight: a refund PayPal has not answered yet still counts against the cap."""
        rows = await self.s.execute(
            select(Action.params["amount"].as_string()).where(
                Action.incident_id == incident.id,
                Action.type == ActionType.REFUND,
                Action.status.in_([ActionStatus.EXECUTED, ActionStatus.IN_FLIGHT]),
            )
        )
        return sum((Decimal(a) for a in rows.scalars() if a), Decimal("0"))

    async def refund_matched(self, incident: Incident, p: PlannedAction) -> list[Action]:
        """Commits after each capture. The capture row is locked and re-read first: an owner approve or
        another incident refunding the same capture waits, then sees REFUNDED; the partial unique index on
        the refund's attempt id stops any second claim that gets past that. Only a STRONG match is refunded
        without the owner; a weak one, or a capture that passed our 3-D Secure (bots never do), waits for
        approval. A crash mid-way keeps the refunds already made on record, and an in-flight one is re-sent
        by the sweeper with the same refund key, which PayPal treats as a repeat."""
        out: list[Action] = []
        for m in await self._matches(incident):
            # Re-read per capture: the guard commit released the incident lock, so the owner may have closed
            # the incident or flipped the kill switch / autopilot since the policy decided. The incident is
            # share-locked until this capture's claim is committed, so an owner close waits for at most one
            # refund (a live one is then already on record as in flight) and never reports "closed" while
            # new refunds keep being started.
            status = (
                await self.s.execute(
                    select(Incident.status).where(Incident.id == incident.id).with_for_update(read=True)
                )
            ).scalar_one()
            ms = await self.s.get(MerchantSettings, incident.shop_id, populate_existing=True)
            assert ms is not None
            if status != IncidentStatus.OPEN or ms.kill_switch:
                audit(
                    self.s,
                    "executor",
                    "refunds.stopped",
                    incident.id,
                    why="incident closed" if status != IncidentStatus.OPEN else "kill switch on",
                )
                await self.s.commit()
                break
            attempt = await self.s.get(
                CheckoutAttempt, m.attempt_id, with_for_update=True, populate_existing=True
            )
            if (
                attempt is None
                or attempt.outcome != Outcome.CAPTURED
                or attempt.disputed
                or (await self.s.execute(self._handled(incident.shop_id, m.attempt_id))).first()
            ):
                await self.s.commit()
                continue
            total = await self._refunded_total(incident)
            # Passed 3-D Secure: captured under the guard's SCA_ALWAYS, or a browser card whose cardholder the
            # bank verified (liability YES) in normal mode. A bot can't pass the bank's challenge, so these
            # are never auto-refunded. POSSIBLE is not enough here: it usually means attempts processing
            # (issuer not participating, nobody challenged), which a bot with a stolen card can reach.
            sca_passed = (
                attempt.guard_decision == Decision.ALLOW_WITH_SCA or attempt.sca_liability_shift == "YES"
            )
            params = {
                "attempt_id": attempt.id,
                "capture_id": attempt.capture_id,
                "amount": str(attempt.amount),
                "match": "strong" if m.strong else "weak",
                "matched_on": m.matched_on,
                "sca_passed": sca_passed,
            }
            over_cap = (
                attempt.amount > ms.max_auto_refund_per_txn
                or total + attempt.amount > ms.max_auto_refund_per_incident
            )
            needs_owner = p.needs_approval or not ms.autopilot  # autopilot may have been turned off since
            # The most telling reason first: the owner sees "passed 3-D Secure" or "weak match" even when
            # autopilot is off or the verdict is stale, so the warning never hides behind a generic rule.
            if sca_passed:
                rule: str | None = "R5_sca_passed_needs_owner"
            elif not m.strong:
                rule = "R5_weak_match_needs_owner"
            elif needs_owner:
                rule = p.rule
            elif over_cap:
                rule = "R5_over_cap_needs_owner"
            else:
                rule = None
            if rule is not None:
                action = await self._record_refund(incident, rule, ActionStatus.AWAITING_APPROVAL, params)
            else:
                action = await self.execute_refund(incident, attempt, params, p.rule, approved_by="auto")
            if action is not None:
                out.append(action)
            await self.s.commit()
        return out

    async def _record_refund(
        self, incident: Incident, rule: str, status: ActionStatus, params: dict[str, Any], **kw: Any
    ) -> Action | None:
        """Insert a refund claim in a savepoint. None when the capture is already claimed (the partial
        unique index uq_actions_refund_attempt): another incident or the owner got there first."""
        action = Action(
            incident_id=incident.id,
            type=ActionType.REFUND,
            policy_rule=rule,
            status=status,
            params=params,
            **kw,
        )
        try:
            async with self.s.begin_nested():
                self.s.add(action)
                await self.s.flush()
        except IntegrityError:
            audit(self.s, "executor", "refund.already_claimed", incident.id, attempt_id=params["attempt_id"])
            return None
        audit(
            self.s,
            "executor",
            f"action.{status}",
            incident.id,
            type=ActionType.REFUND,
            rule=rule,
            params=params,
        )
        await publish(self.s, "action", action_event(action))
        return action

    async def execute_refund(
        self,
        incident: Incident,
        attempt: CheckoutAttempt,
        params: dict[str, Any],
        rule: str,
        approved_by: str,
        pending: Action | None = None,
    ) -> Action | None:
        """Refund one capture. `pending` is an awaiting-approval action being approved by the owner.
        A live refund is committed as IN_FLIGHT before PayPal is called (so a crash or timeout leaves it on
        record for the sweeper), then settled. None when another claim on the capture won."""
        if not attempt.paypal_live:
            # Offline-simulated capture: there is nothing at PayPal to refund. Labelled as simulated.
            ref = f"SIMREF-{uuid.uuid4().hex[:10].upper()}"
            params = {**params, "simulated": True}
            if pending is None:
                action = await self._record_refund(
                    incident, rule, ActionStatus.EXECUTED, params, paypal_ref=ref, approved_by=approved_by
                )
                if action is None:
                    return None
            else:
                action = pending
                action.status, action.paypal_ref, action.error = ActionStatus.EXECUTED, ref, None
                action.params, action.approved_by = params, approved_by
                audit(self.s, "owner", "refund.executed", incident.id, action_id=action.id)
                await publish(self.s, "action", action_event(action))
            attempt.outcome, attempt.refund_id = Outcome.REFUNDED, ref
            return action

        assert self.paypal is not None
        # Kept in the action params: the key that makes a retried or repeated refund a no-op at PayPal.
        request_id = refund_request_id(attempt.id)
        params = {**params, "request_id": request_id}
        now = datetime.now(UTC)
        if pending is None:
            action = await self._record_refund(
                incident, rule, ActionStatus.IN_FLIGHT, params, approved_by=approved_by, claimed_at=now
            )
            if action is None:
                return None
        else:
            action = pending
            action.status, action.claimed_at, action.error = ActionStatus.IN_FLIGHT, now, None
            action.params, action.approved_by = params, approved_by
            audit(self.s, "owner", "refund.in_flight", incident.id, action_id=action.id)
            await publish(self.s, "action", action_event(action))
        await self.s.commit()  # the claim is on record before any money moves
        res = await refund_capture(self.paypal, attempt.capture_id or "", REFUND_NOTE, request_id)
        return await self._settle(incident, action.id, res, actor="owner" if pending else "executor")

    async def _settle(self, incident: Incident, action_id: str, res: PayPalResponse, actor: str) -> Action:
        """Apply PayPal's answer to an IN_FLIGHT refund. An unknown result (network, 429, 5xx) stays in
        flight for the sweeper; a definitive refusal is FAILED. Caller commits."""
        action = await self.s.get(Action, action_id, with_for_update=True, populate_existing=True)
        assert action is not None
        if action.status != ActionStatus.IN_FLIGHT:
            return action  # the sweeper settled it meanwhile (same refund key: same answer)
        attempt = await self.s.get(
            CheckoutAttempt, action.params["attempt_id"], with_for_update=True, populate_existing=True
        )
        assert attempt is not None
        body = res.json if isinstance(res.json, dict) else {}
        if res.ok:
            attempt.outcome, attempt.refund_id = Outcome.REFUNDED, body.get("id")
            action.status, action.paypal_ref, action.error = ActionStatus.EXECUTED, attempt.refund_id, None
        elif res.status == 0 or res.status == 429 or res.status >= 500:
            log.warning("refund_unknown", capture_id=attempt.capture_id, status=res.status)
            action.error = str(res.error_summary())
            action.claimed_at = datetime.now(UTC)  # sweeper re-sends after SWEEP_AFTER
        else:
            log.warning("refund_failed", capture_id=attempt.capture_id, status=res.status)
            action.status, action.error = ActionStatus.FAILED, str(res.error_summary())
        audit(self.s, actor, f"refund.{action.status}", incident.id, action_id=action.id, http=res.status)
        await publish(self.s, "action", action_event(action))
        return action

    async def sweep_in_flight(self, shop_id: str, now: datetime | None = None) -> list[Action]:
        """Re-send refunds stuck IN_FLIGHT for SWEEP_AFTER (a crash between the claim and PayPal's answer,
        or an answer we never got) with the SAME PayPal-Request-Id: PayPal replays a refund it already made
        and makes one it never received, so either way it is made exactly once. Commits. Skipped while the
        kill switch is on; gives up (FAILED, for the owner to check in PayPal) after MAX_SWEEPS tries."""
        if self.paypal is None:
            return []
        now = now or datetime.now(UTC)
        ms = await self.s.get(MerchantSettings, shop_id, populate_existing=True)
        if ms is None or ms.kill_switch:
            return []
        ids = (
            await self.s.execute(
                select(Action.id)
                .join(Incident, Incident.id == Action.incident_id)
                .where(
                    Incident.shop_id == shop_id,
                    Action.type == ActionType.REFUND,
                    Action.status == ActionStatus.IN_FLIGHT,
                    Action.claimed_at < now - SWEEP_AFTER,
                )
                .order_by(Action.claimed_at)
                .limit(20)
            )
        ).scalars()
        out: list[Action] = []
        for action_id in list(ids):
            action = await self.s.get(Action, action_id, with_for_update=True, populate_existing=True)
            if (
                action is None
                or action.status != ActionStatus.IN_FLIGHT
                or action.claimed_at is None
                or action.claimed_at >= now - SWEEP_AFTER
            ):
                await self.s.commit()
                continue
            incident = await self.s.get(Incident, action.incident_id)
            assert incident is not None
            attempt = await self.s.get(
                CheckoutAttempt, action.params["attempt_id"], with_for_update=True, populate_existing=True
            )
            if attempt is not None and attempt.disputed and attempt.outcome != Outcome.REFUNDED:
                # PayPal may never have received this refund (crash before the call), and the buyer can now
                # be paid through the dispute: re-sending could pay twice. The owner settles it in PayPal.
                action.status = ActionStatus.FAILED
                action.error = "disputed while the refund was in flight: check the capture in PayPal"
                audit(self.s, "system", "refund.sweep_disputed", incident.id, action_id=action.id)
                await publish(self.s, "action", action_event(action))
                await self.s.commit()
                out.append(action)
                continue
            sweeps = int(action.params.get("sweeps", 0)) + 1
            action.params = {**action.params, "sweeps": sweeps}
            action.claimed_at = now
            if sweeps > MAX_SWEEPS:
                action.status = ActionStatus.FAILED
                action.error = (
                    f"no answer from PayPal after {MAX_SWEEPS} retries: check the capture in PayPal"
                )
                audit(self.s, "system", "refund.sweep_gave_up", incident.id, action_id=action.id)
                await publish(self.s, "action", action_event(action))
                await self.s.commit()
                out.append(action)
                continue
            audit(self.s, "system", "refund.swept", incident.id, action_id=action.id, sweep=sweeps)
            await self.s.commit()
            res = await refund_capture(
                self.paypal, action.params["capture_id"] or "", REFUND_NOTE, action.params["request_id"]
            )
            out.append(await self._settle(incident, action.id, res, actor="system"))
            await self.s.commit()
        return out

    async def approve(self, incident: Incident, action: Action, override_dispute: bool = False) -> Action:
        """Commits for a live refund (the claim goes in flight before PayPal is called). Raises
        DisputedCapture for a disputed capture unless the owner explicitly overrides."""
        # Locked and re-read: an autopilot refund or another incident's approval of the same capture may
        # have just refunded it (same refund key, so PayPal would replay that refund as if it were ours).
        attempt = await self.s.get(
            CheckoutAttempt, action.params["attempt_id"], with_for_update=True, populate_existing=True
        )
        if attempt is None or attempt.outcome != Outcome.CAPTURED:
            action.status = ActionStatus.REJECTED
            action.error = "capture is no longer refundable"
            return action
        if attempt.disputed:
            if not override_dispute:
                raise DisputedCapture(attempt.dispute_status)
            audit(
                self.s,
                "owner",
                "refund.dispute_override",
                incident.id,
                action_id=action.id,
                dispute_status=attempt.dispute_status,
            )
        done = await self.execute_refund(
            incident, attempt, action.params, action.policy_rule, approved_by="owner", pending=action
        )
        assert done is not None  # the pending row is the claim itself
        return done

    async def reject(self, incident: Incident, action: Action) -> Action:
        action.status = ActionStatus.REJECTED
        action.approved_by = "owner"
        audit(self.s, "owner", "refund.rejected", incident.id, action_id=action.id)
        await publish(self.s, "action", action_event(action))
        return action

    async def deescalate(self, incident: Incident, rule: str) -> None:
        gs = await self.s.get(GuardState, incident.shop_id, with_for_update=True)
        assert gs is not None
        prev = incident.previous_guard_state or {"mode": GuardMode.NORMAL, "held_skus": []}
        gs.mode = prev["mode"]
        gs.held_skus = prev["held_skus"]
        # Device blocks stay until they expire (app/guard/devices.py): a bot that comes back is still blocked.
        gs.set_by_incident = None
        gs.updated_at = datetime.now(UTC)
        invalidate_cache(incident.shop_id)
        await self._record(
            incident, ActionType.DEESCALATE, rule, ActionStatus.EXECUTED, params={"restored": prev["mode"]}
        )


def action_event(a: Action) -> dict[str, Any]:
    return {
        "id": a.id,
        "incident_id": a.incident_id,
        "type": a.type,
        "status": a.status,
        "rule": a.policy_rule,
        "params": a.params,
        "paypal_ref": a.paypal_ref,
        "error": a.error,
        "approved_by": a.approved_by,
        "created_at": a.created_at,
    }
