"""Payment holds: void the attack's payments instead of capturing and refunding them.

The guard holds a first-time shopper's order (app/guard/check.py): it is created with intent AUTHORIZE, so
the bank approves the payment but nothing settles. In normal times that is the owner's review window
(MerchantSettings.review_hold_min, 10 min by default); during an incident the hold lasts until it is judged.
The window starts at checkout, so the payments an attack makes before anything is detected are still held
when it is confirmed. Then:

  - VOID: when the policy allows refunds for the incident (the same gate as R4/R5: an attack verdict the
    scorecard is sure of), a hold the attack matches is voided. A STRONG match (a device with several cards or
    payers, or one already blocked as a bot) is voided at once; a weak one waits for the owner. A void moves
    no money: no PayPal fee is lost (a refund keeps PayPal's fixed fee), nothing settles that the cardholder
    could later dispute, and the owner's money never goes out and back.
  - CAPTURE: every other hold is captured, so a real customer is charged as usual, only a little later: once
    its review window has passed with no open incident covering it, once the incident covering it is closed
    or judged harmless, once the owner keeps it ("Keep" on a pending void), and at the latest after HOLD_MAX
    (an authorization stays honoured for 3 days at PayPal; we settle well before).

A hold does not stop a card tester learning that a card works (the bank approved it): 3-D Secure does that.
Holds stop the money, the fee and the dispute. Offline-simulated holds (SIMAUTH-*) settle without PayPal.
A live call whose answer is unknown (network, 429, 5xx) leaves the hold as it is; the next pass sends the
same request again (same PayPal-Request-Id), which PayPal treats as a repeat.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import func, or_, select

from app import push
from app.db.audit import audit
from app.db.models import (
    Action,
    ActionStatus,
    ActionType,
    CheckoutAttempt,
    Incident,
    MerchantSettings,
    Outcome,
)
from app.events import publish
from app.guard import devices
from app.incidents.service import open_incident
from app.paypal.orders import capture_authorization, hold_request_id, void_authorization
from app.policy.engine import CONFIDENCE_ACT, HARMLESS_CLASSES, PlannedAction
from app.shop.service import attempt_event

if TYPE_CHECKING:
    from app.actions.executor import Executor

log = structlog.get_logger()

HOLD_MAX = timedelta(hours=24)
MAX_VOID_FAILURES = 3  # definitive PayPal refusals before a hold stops being re-tried for a void
SWEEP_LIMIT = 200  # holds settled per pass


def _handled_voids(shop_id: str) -> Any:
    """Attempt ids with a void decision on record (voided, awaiting the owner, or kept by the owner)."""
    aid = Action.params["attempt_id"].as_string()
    return (
        select(aid)
        .join(Incident, Incident.id == Action.incident_id)
        .where(Incident.shop_id == shop_id, Action.type == ActionType.VOID, aid.is_not(None))
        .group_by(aid)
        .having(or_(func.bool_or(Action.status != ActionStatus.FAILED), func.count() >= MAX_VOID_FAILURES))
    )


class Holds:
    def __init__(self, executor: "Executor"):
        self.ex = executor
        self.s = executor.s
        self.paypal = executor.paypal

    # ---- void the attack's holds ------------------------------------------------------------------------

    async def void_matched(self, incident: Incident, p: PlannedAction) -> list[Action]:
        """Called when the policy plans refunds for this incident: its holds are voided instead. Commits
        after each hold."""
        from app.actions.executor import REFUND_LOOKBACK, classify_match

        sig = incident.signature or {}
        top_sku = sig.get("top_sku")
        now = datetime.now(UTC)
        trusted = await devices.trusted(self.s, incident.shop_id, now)
        blocked = await devices.blocked_as_strong(self.s, incident.shop_id, now, incident.id)
        q = (
            select(
                CheckoutAttempt.id,
                CheckoutAttempt.fingerprint_hash,
                CheckoutAttempt.email_domain,
                CheckoutAttempt.sku,
            )
            .where(
                CheckoutAttempt.shop_id == incident.shop_id,
                # Every hold in the incident's window, including the review holds made before it was detected.
                CheckoutAttempt.ts >= incident.opened_at - REFUND_LOOKBACK,
                CheckoutAttempt.outcome == Outcome.AUTHORIZED,
                CheckoutAttempt.id.not_in(_handled_voids(incident.shop_id)),
            )
            .order_by(CheckoutAttempt.ts)
        )
        if trusted:
            q = q.where(CheckoutAttempt.fingerprint_hash.not_in(trusted))
        out: list[Action] = []
        for attempt_id, fp, domain, sku in (await self.s.execute(q)).all():
            m = classify_match(fp, domain, sig, blocked)
            if m is None:
                continue
            strong, matched_on = m
            if not strong and (top_sku is None or sku != top_sku):
                continue  # weak matches only on the attacked item, as for refunds
            attempt = await self.s.get(
                CheckoutAttempt, attempt_id, with_for_update=True, populate_existing=True
            )
            ms = await self.s.get(MerchantSettings, incident.shop_id, populate_existing=True)
            if attempt is None or attempt.outcome != Outcome.AUTHORIZED or ms is None or ms.kill_switch:
                await self.s.commit()
                continue
            params = {
                "attempt_id": attempt.id,
                "authorization_id": attempt.authorization_id,
                "amount": str(attempt.amount),
                "match": "strong" if strong else "weak",
                "matched_on": matched_on,
            }
            if not strong:
                rule: str | None = "R5_weak_match_needs_owner"
            elif p.needs_approval or not ms.autopilot:
                rule = p.rule
            else:
                rule = None
            if rule is not None:
                action = await self.ex._record(
                    incident, ActionType.VOID, rule, ActionStatus.AWAITING_APPROVAL, params=params
                )
                push.queue(
                    self.s,
                    "approval",
                    "Approval needed: {n} payment{s}",
                    "Above your limits, so Checkout Guard waits for you. Open it to approve or reject.",
                    f"/guard/incidents/{incident.id}",
                    f"approval-{incident.id}",
                )
            else:
                action = await self.void(incident, attempt, params, p.rule, approved_by="auto")
            out.append(action)
            await self.s.commit()
        return out

    async def void(
        self,
        incident: Incident,
        attempt: CheckoutAttempt,
        params: dict[str, Any],
        rule: str,
        approved_by: str,
        pending: Action | None = None,
    ) -> Action:
        """Void one hold (attempt row locked by the caller). Caller commits."""
        if not attempt.paypal_live or not self.paypal:
            ref: str | None = f"SIMVOID-{uuid.uuid4().hex[:10].upper()}"
            params = {**params, "simulated": True}
            ok, error = True, None
        else:
            res = await void_authorization(
                self.paypal, attempt.authorization_id or "", hold_request_id(attempt.id, "void")
            )
            ok = res.ok
            ref = attempt.authorization_id if ok else None
            error = None if ok else str(res.error_summary())
            if not ok:
                log.warning("hold_void_failed", attempt=attempt.id, status=res.status)
        status = ActionStatus.EXECUTED if ok else ActionStatus.FAILED
        if pending is None:
            action = await self.ex._record(
                incident,
                ActionType.VOID,
                rule,
                status,
                params=params,
                paypal_ref=ref,
                error=error,
                approved_by=approved_by,
            )
        else:
            action = pending
            action.status, action.paypal_ref, action.error = status, ref, error
            action.params, action.approved_by = params, approved_by
            audit(self.s, "owner", f"void.{status}", incident.id, action_id=action.id)
            await publish(self.s, "action", _event(action))
        if ok:
            attempt.outcome = Outcome.VOIDED
            await publish(self.s, "attempt", attempt_event(attempt))
        return action

    # ---- capture everything else ------------------------------------------------------------------------

    async def capture(self, attempt: CheckoutAttempt, why: str) -> bool:
        """Settle one hold (row locked by the caller): the customer is charged. False when PayPal's answer is
        unknown (left held, retried next pass). Caller commits."""
        if not attempt.paypal_live or not self.paypal:
            attempt.outcome = Outcome.CAPTURED
            attempt.capture_id = f"SIMCAP-{uuid.uuid4().hex[:10].upper()}"
        else:
            res = await capture_authorization(
                self.paypal, attempt.authorization_id or "", hold_request_id(attempt.id, "capture")
            )
            body = res.json if isinstance(res.json, dict) else {}
            if res.ok and body.get("status") in {"COMPLETED", "PENDING"}:
                attempt.outcome, attempt.capture_id = Outcome.CAPTURED, body.get("id")
            elif res.ok or (400 <= res.status < 500 and res.status != 429):
                # Refused (expired or voided at PayPal, or the bank declined the capture): nothing settled.
                log.warning(
                    "hold_capture_refused", attempt=attempt.id, status=res.status, body=body.get("status")
                )
                attempt.outcome = Outcome.DECLINED
                attempt.decline_code = body.get("status") or "AUTHORIZATION_CAPTURE_REFUSED"
            else:
                log.warning("hold_capture_unknown", attempt=attempt.id, status=res.status)
                return False
        audit(self.s, "system", "hold.captured", attempt.hold_incident_id, attempt_id=attempt.id, why=why)
        await publish(self.s, "attempt", attempt_event(attempt))
        return True

    async def settle(self, shop_id: str, now: datetime | None = None) -> int:
        """Worker pass: capture every hold that should now settle. Commits per hold; returns how many."""
        now = now or datetime.now(UTC)
        rows = (
            (
                await self.s.execute(
                    select(CheckoutAttempt.id)
                    .where(CheckoutAttempt.shop_id == shop_id, CheckoutAttempt.outcome == Outcome.AUTHORIZED)
                    .order_by(CheckoutAttempt.ts)
                    .limit(SWEEP_LIMIT)
                )
            )
            .scalars()
            .all()
        )
        ms = await self.s.get(MerchantSettings, shop_id)
        review = timedelta(minutes=ms.review_hold_min if ms else 0)
        settled = 0
        for attempt_id in rows:
            attempt = await self.s.get(
                CheckoutAttempt, attempt_id, with_for_update=True, populate_existing=True
            )
            if attempt is None or attempt.outcome != Outcome.AUTHORIZED:
                await self.s.commit()
                continue
            why = await self._release_reason(attempt, now, review)
            if why is None or not await self.capture(attempt, why):
                await self.s.commit()
                continue
            settled += 1
            await self.s.commit()
        return settled

    async def _release_reason(self, attempt: CheckoutAttempt, now: datetime, review: timedelta) -> str | None:
        voids = (
            (
                await self.s.execute(
                    select(Action).where(
                        Action.type == ActionType.VOID,
                        Action.params["attempt_id"].as_string() == attempt.id,
                        Action.status.in_([ActionStatus.AWAITING_APPROVAL, ActionStatus.REJECTED]),
                    )
                )
            )
            .scalars()
            .all()
        )
        expired = now - attempt.ts > HOLD_MAX
        pending = [a for a in voids if a.status == ActionStatus.AWAITING_APPROVAL]
        if pending:
            if not expired:
                return None  # the owner decides first
            for a in pending:  # nobody decided within HOLD_MAX: the customer is charged, not left hanging
                a.status, a.error = ActionStatus.REJECTED, "hold expired: captured"
                await publish(self.s, "action", _event(a))
            return "expired"
        if any(a.status == ActionStatus.REJECTED for a in voids):
            return "owner_kept"
        if expired:
            return "expired"
        from app.actions.executor import REFUND_LOOKBACK

        current = await open_incident(self.s, attempt.shop_id)
        if current is not None and attempt.ts >= current.opened_at - REFUND_LOOKBACK:
            # The open incident covers this payment: it decides first (void if the attack matches it).
            if current.classification in HARMLESS_CLASSES and (current.confidence or 0) >= CONFIDENCE_ACT:
                return "judged_harmless"
            return None
        if attempt.hold_reason == "incident":
            return "incident_closed"
        return "review_passed" if now - attempt.ts >= review else None

    async def owner_kept(self, incident: Incident, action: Action) -> None:
        """The owner chose Keep on a pending void: capture that hold now (row lock, caller commits)."""
        attempt = await self.s.get(
            CheckoutAttempt, action.params["attempt_id"], with_for_update=True, populate_existing=True
        )
        if attempt is not None and attempt.outcome == Outcome.AUTHORIZED:
            await self.capture(attempt, "owner_kept")


def _event(a: Action) -> dict[str, Any]:
    from app.actions.executor import action_event

    return action_event(a)
