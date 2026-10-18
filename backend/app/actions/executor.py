"""Applies policy-approved actions: guard-state changes and capped refunds through PayPal."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.audit import audit
from app.db.models import (
    Action,
    ActionStatus,
    ActionType,
    CheckoutAttempt,
    GuardMode,
    GuardState,
    Incident,
    IncidentStatus,
    MerchantSettings,
    Outcome,
)
from app.events import publish
from app.guard.check import invalidate_cache
from app.paypal.client import PayPalClient
from app.paypal.orders import refund_capture, refund_request_id
from app.policy.engine import PlannedAction

log = structlog.get_logger()
REFUND_LOOKBACK = timedelta(minutes=15)  # captures shortly before the incident opened still count
REFUND_NOTE = "Refunded by the merchant: this payment was flagged as unauthorised card testing."


def snapshot(gs: GuardState) -> dict[str, Any]:
    return {
        "mode": gs.mode,
        "blocked_fingerprints": list(gs.blocked_fingerprints or []),
        "held_skus": list(gs.held_skus or []),
    }


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
                new = sorted(set(p.params["fingerprints"]) - set(gs.blocked_fingerprints or []))
                if new:
                    gs.blocked_fingerprints = sorted(set(gs.blocked_fingerprints or []) | set(new))
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
    def _claimed(incident: Incident) -> Select[tuple[str | None]]:
        """Attempt ids this incident already has a refund action for (any status)."""
        return select(Action.params["attempt_id"].as_string()).where(
            Action.incident_id == incident.id,
            Action.type == ActionType.REFUND,
            Action.params["attempt_id"].as_string().is_not(None),
        )

    async def _matched_capture_ids(self, incident: Incident) -> list[str]:
        """Candidates only, unlocked: each one is locked and re-checked in its own transaction."""
        sig = incident.signature or {}
        if not sig.get("top_sku"):
            return []
        conds = []
        if sig.get("fingerprints"):
            conds.append(CheckoutAttempt.fingerprint_hash.in_(sig["fingerprints"]))
        if sig.get("new_email_domains"):
            conds.append(CheckoutAttempt.email_domain.in_(sig["new_email_domains"]))
        if not conds:
            return []
        q = (
            select(CheckoutAttempt.id)
            .where(
                CheckoutAttempt.shop_id == incident.shop_id,
                CheckoutAttempt.outcome == Outcome.CAPTURED,
                CheckoutAttempt.capture_id.is_not(None),
                CheckoutAttempt.ts >= incident.opened_at - REFUND_LOOKBACK,
                CheckoutAttempt.sku == sig["top_sku"],
                or_(*conds),
                CheckoutAttempt.id.not_in(self._claimed(incident)),
            )
            .order_by(CheckoutAttempt.ts)
        )
        return list((await self.s.execute(q)).scalars())

    async def _refunded_total(self, incident: Incident) -> Decimal:
        rows = await self.s.execute(
            select(Action.params["amount"].as_string()).where(
                Action.incident_id == incident.id,
                Action.type == ActionType.REFUND,
                Action.status == ActionStatus.EXECUTED,
            )
        )
        return sum((Decimal(a) for a in rows.scalars() if a), Decimal("0"))

    async def refund_matched(self, incident: Incident, p: PlannedAction) -> list[Action]:
        """Commits after each capture. The capture row is locked and re-read first: an owner approve or
        another incident refunding the same capture waits, then sees REFUNDED, so only one action ever
        claims it. A crash mid-way keeps the refunds already made on record; the next run re-sends the
        same refund key, which PayPal treats as a repeat."""
        out: list[Action] = []
        for attempt_id in await self._matched_capture_ids(incident):
            # Re-read per capture: the guard commit released the incident lock, so the owner may have closed
            # the incident or flipped the kill switch / autopilot since the policy decided. The incident is
            # share-locked until this capture's commit, so an owner close waits for at most one refund and
            # never reports "closed" while refunds keep landing.
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
                CheckoutAttempt, attempt_id, with_for_update=True, populate_existing=True
            )
            claimed = (await self.s.execute(self._claimed(incident))).scalars()
            if attempt is None or attempt.outcome != Outcome.CAPTURED or attempt_id in set(claimed):
                await self.s.commit()
                continue
            total = await self._refunded_total(incident)
            params = {
                "attempt_id": attempt.id,
                "capture_id": attempt.capture_id,
                "amount": str(attempt.amount),
            }
            over_cap = (
                attempt.amount > ms.max_auto_refund_per_txn
                or total + attempt.amount > ms.max_auto_refund_per_incident
            )
            needs_owner = p.needs_approval or not ms.autopilot  # autopilot may have been turned off since
            if needs_owner or over_cap:
                rule = p.rule if needs_owner else "R5_over_cap_needs_owner"
                out.append(
                    await self._record(
                        incident, ActionType.REFUND, rule, ActionStatus.AWAITING_APPROVAL, params=params
                    )
                )
            else:
                out.append(await self.execute_refund(incident, attempt, params, p.rule, approved_by="auto"))
            await self.s.commit()
        return out

    async def execute_refund(
        self,
        incident: Incident,
        attempt: CheckoutAttempt,
        params: dict[str, Any],
        rule: str,
        approved_by: str,
        pending: Action | None = None,
    ) -> Action:
        """Refund one capture. `pending` is an awaiting-approval action being approved by the owner."""
        if not attempt.paypal_live:
            # Offline-simulated capture: there is nothing at PayPal to refund. Labelled as simulated.
            attempt.outcome = Outcome.REFUNDED
            attempt.refund_id = f"SIMREF-{uuid.uuid4().hex[:10].upper()}"
            status, ref, error, params = (
                ActionStatus.EXECUTED,
                attempt.refund_id,
                None,
                {**params, "simulated": True},
            )
        else:
            assert self.paypal is not None
            # Kept in the action params: the key that makes a retried or repeated refund a no-op at PayPal.
            request_id = refund_request_id(attempt.id)
            params = {**params, "request_id": request_id}
            res = await refund_capture(self.paypal, attempt.capture_id or "", REFUND_NOTE, request_id)
            body = res.json if isinstance(res.json, dict) else {}
            if res.ok:
                attempt.outcome = Outcome.REFUNDED
                attempt.refund_id = body.get("id")
                status, ref, error = ActionStatus.EXECUTED, attempt.refund_id, None
            else:
                log.warning("refund_failed", capture_id=attempt.capture_id, status=res.status)
                status, ref, error = ActionStatus.FAILED, None, str(res.error_summary())

        if pending is None:
            return await self._record(
                incident,
                ActionType.REFUND,
                rule,
                status,
                params=params,
                paypal_ref=ref,
                error=error,
                approved_by=approved_by,
            )
        pending.status, pending.paypal_ref, pending.error = status, ref, error
        pending.params, pending.approved_by = params, approved_by
        audit(self.s, "owner", f"refund.{status}", incident.id, action_id=pending.id)
        await publish(self.s, "action", action_event(pending))
        return pending

    async def approve(self, incident: Incident, action: Action) -> Action:
        # Locked and re-read: an autopilot refund or another incident's approval of the same capture may
        # have just refunded it (same refund key, so PayPal would replay that refund as if it were ours).
        attempt = await self.s.get(
            CheckoutAttempt, action.params["attempt_id"], with_for_update=True, populate_existing=True
        )
        if attempt is None or attempt.outcome != Outcome.CAPTURED:
            action.status = ActionStatus.REJECTED
            action.error = "capture is no longer refundable"
            return action
        return await self.execute_refund(
            incident, attempt, action.params, action.policy_rule, approved_by="owner", pending=action
        )

    async def reject(self, incident: Incident, action: Action) -> Action:
        action.status = ActionStatus.REJECTED
        action.approved_by = "owner"
        audit(self.s, "owner", "refund.rejected", incident.id, action_id=action.id)
        await publish(self.s, "action", action_event(action))
        return action

    async def deescalate(self, incident: Incident, rule: str) -> None:
        gs = await self.s.get(GuardState, incident.shop_id, with_for_update=True)
        assert gs is not None
        prev = incident.previous_guard_state or {
            "mode": GuardMode.NORMAL,
            "blocked_fingerprints": [],
            "held_skus": [],
        }
        gs.mode = prev["mode"]
        gs.held_skus = prev["held_skus"]
        # Keep blocking the attack's fingerprints: a bot that comes back gets blocked again immediately.
        gs.blocked_fingerprints = sorted(set(gs.blocked_fingerprints or []))
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
        "approved_by": a.approved_by,
        "created_at": a.created_at,
    }
