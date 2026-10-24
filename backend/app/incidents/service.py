"""Incident lifecycle: detect → judge (AI) → decide (policy) → act → explain → stand down.

Transactions: nothing here holds a transaction or a row lock across a Gemini or PayPal call. A new incident
is committed (visible on the dashboard) before the analyst runs; the analyst runs with no transaction open
and its verdict is applied in a fresh one; Executor.apply commits guard changes before any refund; reports
are drafted between two short transactions. The price is that state can change while the AI thinks (the
owner closes the incident, flips the kill switch), so everything is re-read after the call.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import notify
from app.actions.executor import Executor
from app.ai import analyst, gemini, reporter
from app.db.audit import audit
from app.db.models import (
    Action,
    ActionType,
    GuardState,
    Incident,
    IncidentStatus,
    MerchantSettings,
    Outcome,
    Report,
)
from app.detection import baseline as baseline_mod
from app.detection import features as feat
from app.detection import triggers as trig
from app.events import publish
from app.guard import devices
from app.incidents import cadence
from app.paypal.client import PayPalClient
from app.policy.engine import PlannedAction, PolicyInput, decide, rules_first, rules_hint

log = structlog.get_logger()
# An in-flight claim older than this was left by a crashed assessor (the analyst's worst case is two
# rounds of three models at 20 s each).
ASSESS_LEASE = timedelta(minutes=3)


def active_sale_window(ms: MerchantSettings, now: datetime) -> dict[str, Any] | None:
    for w in ms.sale_windows or []:
        try:
            start, end = datetime.fromisoformat(w["start"]), datetime.fromisoformat(w["end"])
        except (KeyError, ValueError):
            continue
        if start <= now <= end:
            return w
    return None


async def open_incident(session: AsyncSession, shop_id: str, lock: bool = False) -> Incident | None:
    q = select(Incident).where(Incident.shop_id == shop_id, Incident.status == IncidentStatus.OPEN)
    q = q.order_by(Incident.opened_at.desc()).limit(1)
    if lock:  # the detector vs an owner close: one waits for the other's (short) transaction
        q = q.with_for_update().execution_options(populate_existing=True)
    return (await session.execute(q)).scalar_one_or_none()


async def release_assessment_claims(session: AsyncSession) -> None:
    """Worker start: it holds the advisory lock, so any claim still set was left by a crashed assessor.
    Those incidents are re-assessed on the next tick (cadence: never assessed, or due as usual)."""
    await session.execute(
        update(Incident).where(Incident.assessing_since.is_not(None)).values(assessing_since=None)
    )
    await session.commit()


class IncidentService:
    def __init__(
        self,
        session: AsyncSession,
        paypal: PayPalClient | None,
        reassess_after: timedelta,
        verdict_max_age: timedelta = cadence.VERDICT_MAX_AGE,
    ):
        self.s = session
        self.paypal = paypal
        self.reassess_after = reassess_after  # base interval; also the AI-failure backoff unit
        self.verdict_max_age = verdict_max_age

    async def tick(self, shop_id: str, now: datetime | None = None) -> Incident | None:
        """One detector pass for a shop. Safe to call repeatedly. Commits (several times)."""
        now = now or datetime.now(UTC)
        ms = await self.s.get(MerchantSettings, shop_id)
        if ms is None:
            return None
        rows = await baseline_mod.recent_rows(self.s, shop_id, now - timedelta(minutes=60))
        base = await baseline_mod.load_baseline(self.s, shop_id, now)
        features = feat.compute_all(rows, now, base)
        fired = trig.evaluate(features, base)
        incident = await open_incident(self.s, shop_id, lock=True)

        if incident is None:
            if not fired:
                return None
            incident = Incident(shop_id=shop_id, opened_at=now, last_signal_at=now, triggers=fired)
            self.s.add(incident)
            await self.s.flush()
            audit(self.s, "detector", "incident.opened", incident.id, triggers=fired)
            await publish(self.s, "incident.opened", incident_event(incident))
            await self.s.commit()  # on the dashboard ("investigating") before the AI has answered
            log.info("incident_opened", incident=incident.id, triggers=fired)
            await self._assess(incident, ms, rows, base, features, fired, now, reason="first_verdict")
        else:
            if fired:
                incident.last_signal_at = now
                incident.triggers = sorted(set(incident.triggers or []) | set(fired))
            quiet_for = now - incident.last_signal_at
            if quiet_for >= timedelta(minutes=ms.deescalate_after_min) and trig.quiet_since(
                rows, now, ms.deescalate_after_min
            ):
                await self.close(incident, "auto_deescalate_quiet")
            elif reason := cadence.reassess_reason(
                incident,
                now=now,
                fired=bool(fired),
                sale_active=active_sale_window(ms, now) is not None,
                attempts_15m=features[15].attempts,
                new_captures=incident.last_assessed_at is not None
                and any(r.outcome == Outcome.CAPTURED and r.ts > incident.last_assessed_at for r in rows),
                base=self.reassess_after,
                max_age=self.verdict_max_age,
            ):
                await self._assess(incident, ms, rows, base, features, fired, now, reason=reason)
            else:
                await self._enforce(incident, ms, rows, base, features, now)
        await self.s.commit()
        return incident

    async def _policy_input(
        self,
        incident: Incident,
        ms: MerchantSettings,
        sig: trig.Signature,
        f15: feat.Features,
        now: datetime,
        triggers: list[str] | None = None,
    ) -> PolicyInput:
        """triggers: the ones to tier; defaults to everything this incident has fired (a fraud signal seen
        once keeps an AI-down incident in R3b, and keeps it out of the volume-only R3c)."""
        gs = await self.s.get(GuardState, incident.shop_id, populate_existing=True)
        return PolicyInput(
            classification=incident.classification or "unclear",
            confidence=incident.confidence or 0.0,
            autopilot=ms.autopilot,
            kill_switch=ms.kill_switch,
            sale_window_active=active_sale_window(ms, now) is not None,
            attack_fingerprints=sig.fingerprints,
            attack_sku=sig.top_sku,
            verdict_stale=cadence.verdict_stale(incident, now),
            ai_available=incident.ai_available,
            trigger_tiers=trig.tiers(incident.triggers or [] if triggers is None else triggers),
            known_customer_share=f15.known_customer_share,
            decline_rate=f15.decline_rate,
            strong_fingerprints=sig.strong_fingerprints,
            guard_set_by_incident=gs is not None and gs.set_by_incident == incident.id,
        )

    async def _plan(
        self, incident: Incident, ms: MerchantSettings, sig: trig.Signature, f15: feat.Features, now: datetime
    ) -> list[PlannedAction]:
        return decide(await self._policy_input(incident, ms, sig, f15, now))

    async def _rules_first(
        self,
        incident: Incident,
        ms: MerchantSettings,
        sig: trig.Signature,
        f15: feat.Features,
        fired: list[str],
        now: datetime,
    ) -> list[Action]:
        """Policy R0: commit 3-D Secure for a fraud-tier signal before the analyst is asked. Commits.
        Re-locks the incident first: a new incident was committed before this (and Executor.apply's commit
        releases the tick's lock), so the owner may have closed it, and a closed incident's checkout must
        not be re-armed with nothing left to undo it."""
        await self.s.flush()  # keep this tick's trigger updates: the refresh would overwrite them
        await self.s.refresh(incident, with_for_update=True)
        if incident.status != IncidentStatus.OPEN:
            return []
        unjudged = cadence.unjudged_triggers(incident, fired)
        planned = rules_first(await self._policy_input(incident, ms, sig, f15, now, triggers=unjudged))
        if not planned:
            return []
        executed = await Executor(self.s, self.paypal).apply(incident, planned)
        if executed:
            audit(
                self.s,
                "policy",
                "rules_first",
                incident.id,
                triggers=sorted(set(unjudged) & trig.FRAUD_TRIGGERS),
                actions=[f"{a.type}:{a.policy_rule}" for a in executed],
            )
            await self.s.commit()
        return executed

    async def _enforce(
        self,
        incident: Incident,
        ms: MerchantSettings,
        rows: list[feat.AttemptRow],
        base: feat.Baseline,
        features: dict[int, feat.Features],
        now: datetime,
    ) -> None:
        """Between AI assessments: re-apply the current verdict's policy to new data (no AI call).
        Catches captures that landed while the AI was deciding and bot devices seen since."""
        if incident.classification is None:
            return
        trusted = await devices.trusted(self.s, incident.shop_id)
        sig = trig.signature(rows, incident.opened_at - timedelta(minutes=15), now, base, trusted)
        incident.signature = sig.as_dict()
        planned = [
            p for p in await self._plan(incident, ms, sig, features[15], now) if p.type != ActionType.NOTIFY
        ]
        executed = await Executor(self.s, self.paypal).apply(incident, planned)
        if executed:
            audit(
                self.s, "policy", "enforced", incident.id, actions=[f"{a.type}:{a.status}" for a in executed]
            )

    async def _claim(self, incident: Incident, now: datetime) -> bool:
        """Compare-and-set the in-flight flag, so two assessors never ask the AI about one incident."""
        res = await self.s.execute(
            update(Incident)
            .where(
                Incident.id == incident.id,
                Incident.status == IncidentStatus.OPEN,
                or_(Incident.assessing_since.is_(None), Incident.assessing_since < now - ASSESS_LEASE),
            )
            .values(assessing_since=now)
            .returning(Incident.id)
            .execution_options(synchronize_session=False)
        )
        return res.scalar_one_or_none() is not None

    async def _release(self, incident_id: str) -> None:
        await self.s.rollback()
        await self.s.execute(
            update(Incident)
            .where(Incident.id == incident_id)
            .values(assessing_since=None)
            .execution_options(synchronize_session=False)
        )
        await self.s.commit()

    async def _assess(
        self,
        incident: Incident,
        ms: MerchantSettings,
        rows: list[feat.AttemptRow],
        base: feat.Baseline,
        features: dict[int, feat.Features],
        fired: list[str],
        now: datetime,
        reason: str,
    ) -> None:
        first = reason == "first_verdict"
        gs = await self.s.get(GuardState, incident.shop_id)
        assert gs is not None
        sale = active_sale_window(ms, now)
        trusted = await devices.trusted(self.s, incident.shop_id)
        sig = trig.signature(rows, min(incident.opened_at, now) - timedelta(minutes=15), now, base, trusted)
        rules_acted = await self._rules_first(incident, ms, sig, features[15], fired, now)
        if incident.status != IncidentStatus.OPEN:
            return  # closed by the owner before R0 could act
        await self.s.refresh(gs)  # R0 may have just turned 3-D Secure on: the analyst sees the current state
        prior = (await self.s.execute(select(Action.type).where(Action.incident_id == incident.id))).scalars()

        analyst_input = analyst.build_input(
            features=features,
            baseline=base,
            triggers=fired or list(incident.triggers or []),
            sale_window=sale,
            guard_state={
                "mode": gs.mode,
                "blocked_devices": await devices.count_blocked(self.s, incident.shop_id),
                "held_skus": gs.held_skus,
            },
            actions_so_far=sorted(set(prior)),
            sample=[r for r in rows if r.ts > now - timedelta(minutes=15)],
        )
        if not await self._claim(incident, now):
            # Either another assessor holds the claim, or the incident was closed since R0's commit: only
            # an open incident's verdict is enforced (a closed one's checkout was already restored).
            await self.s.refresh(incident, with_for_update=True)
            if incident.status != IncidentStatus.OPEN:
                return
            log.info("assessment_already_in_flight", incident=incident.id)
            await self._enforce(incident, ms, rows, base, features, now)
            return
        await self.s.commit()  # no transaction, no row lock while Gemini thinks

        try:
            with gemini.track_usage() as calls:
                result = await analyst.analyse(analyst_input, features)
            executed = await self._apply_verdict(
                incident, ms, result, calls, features, base, sig, now, reason
            )
        except BaseException:
            # Until the verdict commit lands, the claim is only cleared in memory: any failure (the AI call,
            # a lock timeout in the guard update) would otherwise leave it set for the whole lease, and an
            # incident with no verdict yet would get no defences at all until it expired.
            await self._release(incident.id)
            raise
        if executed is None:
            return

        # The refunds ran in their own transactions with no incident lock: the owner may have closed it.
        await self.s.refresh(incident)
        if incident.status != IncidentStatus.OPEN:
            return
        await publish(self.s, "incident.updated", incident_event(incident))
        await self.s.commit()

        # Notify the owner on the first verdict and whenever new defences were switched on (by R0 too).
        if first or rules_acted or any(a.type != ActionType.REFUND for a in executed):
            await self.write_report(incident, skip_ai=not result.ai_available)

    async def _apply_verdict(
        self,
        incident: Incident,
        ms: MerchantSettings,
        result: analyst.AnalystResult,
        calls: list[dict[str, Any]],
        features: dict[int, feat.Features],
        base: feat.Baseline,
        sig: trig.Signature,
        now: datetime,
        reason: str,
    ) -> list[Action] | None:
        """Commits (inside Executor.apply). None when the verdict was discarded."""
        # Re-read what may have changed during the call: the owner may have closed the incident or
        # changed settings (kill switch, autopilot, sale windows).
        await self.s.refresh(incident, with_for_update=True)
        await self.s.refresh(ms)
        incident.assessing_since = None
        if calls:
            audit(self.s, "ai", "ai.usage", incident.id, purpose="analyst", calls=calls)
        if incident.status != IncidentStatus.OPEN:
            audit(self.s, "ai", "ai.verdict_discarded", incident.id, why="incident closed during the call")
            await self.s.commit()
            return None

        self._record_verdict(
            incident, result, features, base, sig, now, reason, active_sale_window(ms, now) is not None
        )
        policy_input = await self._policy_input(incident, ms, sig, features[15], now)
        incident.rules_hint = rules_hint(policy_input)
        planned = decide(policy_input)
        audit(self.s, "policy", "decided", incident.id, actions=[f"{p.type}:{p.rule}" for p in planned])
        return await Executor(self.s, self.paypal).apply(incident, planned)

    def _record_verdict(
        self,
        incident: Incident,
        result: analyst.AnalystResult,
        features: dict[int, feat.Features],
        base: feat.Baseline,
        sig: trig.Signature,
        now: datetime,
        reason: str,
        sale_active: bool,
    ) -> None:
        """Store the analyst's answer, or keep the last real AI verdict when a re-check failed: one 429
        mid-attack must not drop blocks and refunds to the rules fallback ("unclear", soft defences only)."""
        incident.features = {f"{m}m": f.as_dict() for m, f in features.items()} | {"baseline": base.public()}
        incident.signature = sig.as_dict()
        incident.last_assessed_at = now
        triggers = list(incident.triggers or [])
        verdict_triggers = (incident.assessment_context or {}).get("verdict_triggers", triggers)

        keep = not result.ai_available and cadence.has_real_verdict(incident)
        if keep and (new := cadence.contradicting_triggers(incident)):
            # Guard 1: a kept verdict must not outlive new signals that contradict it.
            audit(
                self.s,
                "ai",
                "ai.kept_verdict_dropped",
                incident.id,
                kept=incident.classification,
                new_triggers=sorted(new),
            )
            keep = False

        if keep:
            incident.ai_refresh_failed_at = now
            incident.ai_refresh_failures += 1
            audit(
                self.s,
                "ai",
                "ai.refresh_failed",
                incident.id,
                kept=incident.classification,
                confidence=incident.confidence,
                failures=incident.ai_refresh_failures,
                why=result.verdict.uncertainty_notes,
                reask=reason,
            )
        else:
            v = result.verdict
            incident.verdict = v.model_dump()
            incident.classification = v.classification
            incident.confidence = v.confidence
            incident.model_id = result.model_id
            incident.prompt_version = analyst.PROMPT_VERSION
            incident.ai_available = result.ai_available
            incident.verdict_at = now
            incident.ai_refresh_failed_at = None
            incident.ai_refresh_failures = 0 if result.ai_available else incident.ai_refresh_failures + 1
            verdict_triggers = triggers
            audit(
                self.s,
                "ai",
                "verdict",
                incident.id,
                classification=v.classification,
                confidence=v.confidence,
                model=result.model_id,
                dropped_evidence=result.dropped_evidence,
                numbers_replaced=result.numbers_replaced,
                first=reason == "first_verdict",
                reask=reason,
            )
        incident.assessment_context = cadence.context(
            triggers=triggers,
            sale_window=sale_active,
            attempts_15m=features[15].attempts,
            verdict_triggers=verdict_triggers,
        )

    async def write_report(
        self, incident: Incident, skip_ai: bool = False, notify_owner: bool = True
    ) -> Report | None:
        """Commits twice: facts are read in one transaction, Gemini drafts with none open, and the report
        is upserted in another (an upsert: the worker and the owner's regenerate can race). None when the
        incident was closed while drafting: the close writes the final report. notify_owner=False (the
        owner's own regenerate) neither records an alert in the history nor pushes it to the webhook."""
        await self.s.refresh(
            incident
        )  # expire_on_commit=False: the owner may have closed it since we read it
        was_open = incident.status == IncidentStatus.OPEN
        facts = await reporter.gather_facts(self.s, incident)
        await self.s.commit()
        with gemini.track_usage() as calls:
            draft, model_id = await reporter.compose(incident, facts, skip_ai=skip_ai)
        values = {**reporter.render(draft, facts), "model_id": model_id, "generated_at": datetime.now(UTC)}
        # Locked so an owner close waits for this upsert, then writes the final report after it.
        await self.s.refresh(incident, with_for_update=True)
        if was_open and incident.status != IncidentStatus.OPEN:
            # Closed while we drafted: the close wrote (or is writing) the final report; ours says
            # "still being handled" and must not replace it.
            if calls:
                audit(self.s, "ai", "ai.usage", incident.id, purpose="report", calls=calls)
            audit(self.s, "ai", "report.discarded", incident.id, why="incident closed while drafting")
            await self.s.commit()
            return None
        await self.s.execute(
            insert(Report)
            .values(incident_id=incident.id, **values)
            .on_conflict_do_update(index_elements=[Report.incident_id], set_=values)
        )
        if calls:
            audit(self.s, "ai", "ai.usage", incident.id, purpose="report", calls=calls)
        report = await self.s.get(Report, incident.id, populate_existing=True)
        assert report is not None
        if notify_owner:
            # The alert history (GET /dashboard/alerts): every alert the owner was sent, with its text.
            audit(self.s, "system", "alert.sent", incident.id, text=report.alert_text, model_id=model_id)
        await publish(self.s, "alert", {"incident_id": incident.id, "text": report.alert_text})
        await self.s.commit()
        if notify_owner:
            notify.send(report.alert_text or "", incident.id)  # after commit, in the background
        return report

    async def close(self, incident: Incident, rule: str) -> None:
        """Commits: the checkout is restored (and the GuardState lock released) before the report is
        written, which may wait on Gemini."""
        await Executor(self.s, self.paypal).deescalate(incident, rule)
        incident.status = IncidentStatus.CLOSED
        incident.closed_at = datetime.now(UTC)
        audit(self.s, "system", "incident.closed", incident.id, rule=rule)
        await publish(self.s, "incident.closed", incident_event(incident))
        await self.s.commit()
        log.info("incident_closed", incident=incident.id, rule=rule)
        await self.write_report(incident)


def incident_event(i: Incident) -> dict[str, Any]:
    return {
        "id": i.id,
        "status": i.status,
        "opened_at": i.opened_at,
        "closed_at": i.closed_at,
        "classification": i.classification,
        "confidence": i.confidence,
        "summary": (i.verdict or {}).get("summary"),
        "triggers": i.triggers,
        "ai_available": i.ai_available,
        "ai_refresh_failed_at": i.ai_refresh_failed_at,
        "rules_hint": i.rules_hint,
    }
