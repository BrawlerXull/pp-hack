"""Incident lifecycle: detect → judge (scorecard; Gemini breaks ties) → decide (policy) → act → explain
→ stand down.

The classification comes from the deterministic scorecard (app/detection/classifier.py). Gemini writes the
owner-facing explanation, and is asked for a second opinion only when the scorecard is unsure; what that
opinion is worth is app/incidents/decision.combine. With Gemini down or switched off, everything still
works: the scorecard decides and the text is our own template.

Transactions: nothing here holds a transaction or a row lock across a Gemini or PayPal call. A new incident
is committed (visible on the dashboard) before it is assessed; a confident scorecard verdict is applied
before Gemini is called; Gemini runs with no transaction open and its answer is applied in a fresh one;
Executor.apply commits guard changes before any refund; reports are drafted between two short transactions.
The price is that state can change while the AI thinks (the owner closes the incident, flips the kill
switch), so everything is re-read after the call.
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
from app.ai.schemas import Verdict
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
from app.detection import classifier, normal
from app.detection import features as feat
from app.detection import triggers as trig
from app.detection.classifier import ScoreResult
from app.events import publish
from app.guard import devices
from app.incidents import cadence
from app.incidents.decision import (
    DECIDED_BY_AGREE,
    DECIDED_BY_DISAGREE,
    Decision,
    combine,
    scorecard_only,
    unopposed_tiebreak,
)
from app.paypal.client import PayPalClient
from app.policy.engine import PlannedAction, PolicyInput, decide, rules_first, rules_hint

log = structlog.get_logger()
# An in-flight claim older than this was left by a crashed assessor (the analyst's worst case is two
# rounds of three models at 20 s each).
ASSESS_LEASE = timedelta(minutes=3)
# assessment_context flag: a decision's owner alert has not been sent yet (_send_overdue_report).
REPORT_DUE = "report_due"


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
        # The last tick's 5/15-minute numbers and normal, for the worker heartbeat (dashboard live tiles).
        self.live: dict[str, Any] | None = None

    async def tick(self, shop_id: str, now: datetime | None = None) -> Incident | None:
        """One detector pass for a shop. Safe to call repeatedly. Commits (several times)."""
        now = now or datetime.now(UTC)
        ms = await self.s.get(MerchantSettings, shop_id)
        if ms is None:
            return None
        rows = await baseline_mod.recent_rows(self.s, shop_id, now - timedelta(minutes=60))
        base = await baseline_mod.load_baseline(self.s, shop_id, now)
        features = feat.compute_all(rows, now, base)
        self.live = normal.live_snapshot(features, base) | {"at": now.isoformat()}
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
        await self._send_overdue_report(incident)
        return incident

    async def _send_overdue_report(self, incident: Incident) -> None:
        """A decision was applied (and defences switched on) but its alert never went out: the worker died
        or the explanation call raised in between. Sent with our own wording; skipped while an assessor
        holds the claim, since that one sends it when it finishes."""
        await self.s.refresh(incident)
        if (
            incident.status == IncidentStatus.OPEN
            and (incident.assessment_context or {}).get(REPORT_DUE)
            and incident.assessing_since is None
        ):
            await self.s.commit()
            await self.write_report(incident, skip_ai=True)

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
        once keeps an unopposed tie-break in R3b, and keeps it out of the volume-only R3c)."""
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
            unopposed_tiebreak=unopposed_tiebreak(incident),
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
        """Scorecard first, Gemini second. A confident scorecard verdict is applied (and its defences
        committed) before Gemini is asked for the words; an unsure one waits for the AI's second opinion
        (policy R0 already switched on 3-D Secure for fraud signals) unless Gemini is down or backing off."""
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
        score = classifier.classify(features, base, sale_active=sale is not None)
        tiebreak = score.needs_tiebreak
        ask_ai = cadence.ai_due(incident, now, self.reassess_after)
        executed: list[Action] = []
        if not tiebreak:
            # The scorecard is sure: its defences go live now, not after a 3-25 s Gemini call.
            done = await self._apply_decision(incident, ms, score, None, [], features, base, sig, now, reason)
            if done is None:
                return
            executed += done
            # The refunds ran in their own transactions with no incident lock: the owner may have closed it.
            await self.s.refresh(incident)
            if incident.status != IncidentStatus.OPEN:
                return
            if rules_acted or any(a.type != ActionType.REFUND for a in done):
                # Owed an alert for these defences even if the explanation call below never returns.
                incident.assessment_context = (incident.assessment_context or {}) | {REPORT_DUE: True}
            await publish(self.s, "incident.updated", incident_event(incident))  # the dashboard sees it now
            await self.s.commit()

        result: analyst.AnalystResult | None = None
        if ask_ai:
            if await self._claim(incident, now):
                await self.s.commit()  # no transaction, no row lock while Gemini thinks
            else:
                # Either another assessor holds the claim, or the incident was closed since R0's commit:
                # only an open incident's verdict is enforced (a closed one's checkout was already restored).
                await self.s.refresh(incident, with_for_update=True)
                if incident.status != IncidentStatus.OPEN:
                    return
                log.info("assessment_already_in_flight", incident=incident.id)
                if tiebreak:
                    await self._enforce(incident, ms, rows, base, features, now)
                    return
                ask_ai = False  # the decision above stands; the other assessor writes the words

        if ask_ai:
            try:
                with gemini.track_usage() as calls:
                    if tiebreak:
                        result = await analyst.analyse(analyst_input, features)
                    else:
                        assert incident.classification is not None and incident.confidence is not None
                        result = await analyst.explain(
                            analyst_input, features, score, incident.classification, incident.confidence
                        )
                if tiebreak:
                    done = await self._apply_decision(
                        incident, ms, score, result, calls, features, base, sig, now, reason
                    )
                else:
                    done = await self._apply_explanation(incident, score, result, calls, now)
            except BaseException:
                # Until the commit lands, the claim is only cleared in memory: any failure (the AI call, a
                # lock timeout in the guard update) would otherwise leave it set for the whole lease, and an
                # incident with no verdict yet would get no defences at all until it expired.
                await self._release(incident.id)
                raise
            if done is None:
                return
            executed += done
        elif tiebreak:
            # Gemini is backing off after failures: the scorecard's own call stands for now.
            done = await self._apply_decision(incident, ms, score, None, [], features, base, sig, now, reason)
            if done is None:
                return
            executed += done

        # The refunds ran in their own transactions with no incident lock: the owner may have closed it.
        await self.s.refresh(incident)
        if incident.status != IncidentStatus.OPEN:
            return
        await publish(self.s, "incident.updated", incident_event(incident))
        await self.s.commit()

        # Notify the owner on the first verdict and whenever new defences were switched on (by R0 too).
        if first or rules_acted or any(a.type != ActionType.REFUND for a in executed):
            await self.write_report(incident, skip_ai=result is None or not result.ai_available)

    async def _apply_decision(
        self,
        incident: Incident,
        ms: MerchantSettings,
        score: ScoreResult,
        result: analyst.AnalystResult | None,
        calls: list[dict[str, Any]],
        features: dict[int, feat.Features],
        base: feat.Baseline,
        sig: trig.Signature,
        now: datetime,
        reason: str,
    ) -> list[Action] | None:
        """Record the classification and act on it. result: Gemini's second opinion when it was asked (the
        tie-break), None otherwise. Commits (inside Executor.apply). None when the incident was closed."""
        # Re-read what may have changed (during the Gemini call, or since the tick read it): the owner may
        # have closed the incident or changed settings (kill switch, autopilot, sale windows).
        await self.s.refresh(incident, with_for_update=True)
        await self.s.refresh(ms)
        if result is not None:
            incident.assessing_since = None  # our claim (another assessor's claim is never cleared here)
        if calls:
            audit(self.s, "ai", "ai.usage", incident.id, purpose="analyst", calls=calls)
        if incident.status != IncidentStatus.OPEN:
            why = "incident closed during the call" if result is not None else "incident closed"
            audit(self.s, "ai", "ai.verdict_discarded", incident.id, why=why)
            await self.s.commit()
            return None

        self._record_decision(
            incident, score, result, features, base, sig, now, reason, active_sale_window(ms, now) is not None
        )
        policy_input = await self._policy_input(incident, ms, sig, features[15], now)
        incident.rules_hint = rules_hint(policy_input)
        planned = decide(policy_input)
        audit(self.s, "policy", "decided", incident.id, actions=[f"{p.type}:{p.rule}" for p in planned])
        return await Executor(self.s, self.paypal).apply(incident, planned)

    def _record_decision(
        self,
        incident: Incident,
        score: ScoreResult,
        result: analyst.AnalystResult | None,
        features: dict[int, feat.Features],
        base: feat.Baseline,
        sig: trig.Signature,
        now: datetime,
        reason: str,
        sale_active: bool,
    ) -> None:
        """Store the scorecard's decision (with the tie-break, if Gemini was asked). An agreed tie-break
        that Gemini cannot re-confirm is kept instead of dropping to "unclear": one 429 mid-attack must
        not drop blocks to the rules fallback (cadence: Guard 1 and Guard 2 still apply)."""
        incident.features = {f"{m}m": f.as_dict() for m, f in features.items()} | {"baseline": base.public()}
        incident.signature = sig.as_dict()
        incident.last_assessed_at = now
        triggers = list(incident.triggers or [])
        ctx = incident.assessment_context or {}
        verdict_triggers = ctx.get("verdict_triggers", triggers)
        asked = result is not None
        ai_at = now.isoformat() if asked else ctx.get("ai_at")
        switched_off = asked and result.fallback_reason == gemini.AI_DISABLED
        gemini_ok = asked and result.ai_available
        if score.needs_tiebreak:
            decision = combine(
                score,
                result.verdict if gemini_ok else None,
                model_id=result.model_id if gemini_ok else None,
                verified=result.verified if gemini_ok else True,
            )
        else:
            decision = scorecard_only(score)

        # Keep an agreed tie-break the AI could not re-confirm (down, quota, or still backing off). The
        # owner switching the AI off is not a failed re-check: the scorecard's own call applies at once.
        keep = (
            score.needs_tiebreak
            and not gemini_ok
            and not switched_off
            and cadence.has_real_verdict(incident)
            and incident.classification == score.top_class
        )
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

        if asked:
            incident.ai_fallback_reason = None if gemini_ok else result.fallback_reason
        if keep:
            if asked:
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
                why=result.verdict.uncertainty_notes if asked else "waiting to retry the AI",
                reask=reason,
            )
        else:
            verdict, model_id = self._verdict_text(incident, score, decision, result)
            incident.verdict = verdict.model_dump()
            incident.classification = decision.classification
            incident.confidence = round(decision.confidence, 3)  # same rounding as class_probs
            incident.decided_by = decision.decided_by
            incident.classifier_version = score.version
            incident.class_probs = {k: round(v, 3) for k, v in score.probabilities.items()}
            incident.class_margin = round(score.margin, 3)
            incident.scorecard = score.public() | {"tiebreak": decision.tiebreak}
            incident.model_id = model_id
            incident.prompt_version = analyst.PROMPT_VERSION if model_id != cadence.TEMPLATE else None
            incident.verdict_at = now
            incident.verdict_features = incident.features
            incident.ai_refresh_failed_at = None
            if asked:
                incident.ai_available = gemini_ok
                incident.dropped_evidence = result.dropped_evidence if gemini_ok else 0
                if gemini_ok:
                    incident.ai_refresh_failures = 0
                elif not switched_off:
                    incident.ai_refresh_failures += 1
            elif score.needs_tiebreak:
                incident.ai_available = False  # no second opinion this time (backing off)
            verdict_triggers = triggers
            audit(
                self.s,
                "detector",
                "verdict",
                incident.id,
                classification=decision.classification,
                confidence=round(decision.confidence, 3),
                decided_by=decision.decided_by,
                classifier=score.version,
                probabilities=incident.class_probs,
                margin=incident.class_margin,
                reasons=[r.feature or r.text for r in score.top_reasons()],
                tiebreak=decision.tiebreak,
                model=model_id,
                dropped_evidence=result.dropped_evidence if gemini_ok else 0,
                numbers_replaced=result.numbers_replaced if gemini_ok else 0,
                first=reason == "first_verdict",
                reask=reason,
            )
        incident.assessment_context = cadence.context(
            triggers=triggers,
            sale_window=sale_active,
            attempts_15m=features[15].attempts,
            verdict_triggers=verdict_triggers,
            ai_at=ai_at,
        )
        if ctx.get(REPORT_DUE) or reason == "first_verdict":
            # Committed with the verdict: if the worker dies before the alert goes out, the next tick
            # still sends it (tick → _send_overdue_report).
            incident.assessment_context = incident.assessment_context | {REPORT_DUE: True}

    @staticmethod
    def _verdict_text(
        incident: Incident, score: ScoreResult, decision: Decision, result: analyst.AnalystResult | None
    ) -> tuple[Verdict, str]:
        """The words shown with a decision, and who wrote them. The evidence is always the scorecard's
        reasons (our numbers); Gemini contributes the summary and notes when it agreed to a tie-break, and
        explanations arrive later (_apply_explanation)."""
        tiebreak = decision.tiebreak or {}
        if decision.decided_by == DECIDED_BY_AGREE and result is not None:
            v = result.verdict.model_copy(deep=True)
            v.classification, v.confidence = decision.classification, round(decision.confidence, 3)
            v.evidence = score.evidence()
            return v, result.model_id
        if decision.decided_by == DECIDED_BY_DISAGREE:
            note = (
                f"The AI second opinion read this as {str(tiebreak.get('gemini')).replace('_', ' ')}, "
                "so only soft defences apply."
            )
            return analyst.template_verdict("unclear", decision.confidence, score, [note]), cadence.TEMPLATE
        if tiebreak:
            note = (
                "The AI second opinion's evidence did not match our numbers, so the scorecard's call stands."
                if tiebreak.get("outcome") == "unverified"
                else "The AI second opinion was unavailable, so the scorecard's call stands."
            )
            v = analyst.template_verdict(decision.classification, decision.confidence, score, [note])
            return v, cadence.TEMPLATE
        prev = incident.verdict or {}
        if (
            incident.model_id not in (None, cadence.TEMPLATE, cadence.RULES_FALLBACK)
            and incident.classification == decision.classification
            and prev.get("summary")
        ):
            # Same class as before: keep the AI's last wording until the new explanation lands.
            v = analyst.template_verdict(decision.classification, decision.confidence, score)
            v.summary = prev["summary"]
            v.uncertainty_notes = prev.get("uncertainty_notes", "")
            v.recommended_actions = prev.get("recommended_actions", [])
            return v, incident.model_id
        return analyst.template_verdict(decision.classification, decision.confidence, score), cadence.TEMPLATE

    async def _apply_explanation(
        self,
        incident: Incident,
        score: ScoreResult,
        result: analyst.AnalystResult,
        calls: list[dict[str, Any]],
        now: datetime,
    ) -> list[Action] | None:
        """Put Gemini's words on a decision already applied. Never changes the classification, confidence
        or defences. Commits. None when the incident was closed during the call."""
        await self.s.refresh(incident, with_for_update=True)
        incident.assessing_since = None
        if calls:
            audit(self.s, "ai", "ai.usage", incident.id, purpose="analyst", calls=calls)
        if incident.status != IncidentStatus.OPEN:
            audit(
                self.s, "ai", "ai.explanation_discarded", incident.id, why="incident closed during the call"
            )
            await self.s.commit()
            return None
        incident.assessment_context = (incident.assessment_context or {}) | {"ai_at": now.isoformat()}
        if result.ai_available and result.verdict.classification != incident.classification:
            # Another assessor re-decided while Gemini was writing: these words explain an old decision.
            audit(
                self.s, "ai", "ai.explanation_discarded", incident.id, why="decision changed during the call"
            )
            await self.s.commit()
            return []

        switched_off = result.fallback_reason == gemini.AI_DISABLED
        incident.ai_fallback_reason = None if result.ai_available else result.fallback_reason
        current = Verdict.model_validate(incident.verdict)
        if result.ai_available:
            current.summary = result.verdict.summary
            current.uncertainty_notes = result.verdict.uncertainty_notes
            current.recommended_actions = result.verdict.recommended_actions
            current.trend = result.verdict.trend
            incident.verdict = current.model_dump()
            # Words the analyst replaced with our template are ours, not the model's (analyst.explain).
            incident.model_id = cadence.TEMPLATE if result.template_text else result.model_id
            incident.prompt_version = None if result.template_text else analyst.PROMPT_VERSION
            incident.ai_available = True
            incident.ai_refresh_failures = 0
            incident.dropped_evidence = result.dropped_evidence
            audit(
                self.s,
                "ai",
                "ai.explained",
                incident.id,
                model=result.model_id,
                gemini_view=result.gemini_view,
                template_text=result.template_text,
                dropped_evidence=result.dropped_evidence,
                numbers_replaced=result.numbers_replaced,
            )
        else:
            if not switched_off:
                incident.ai_refresh_failures += 1
            gemini_text = incident.model_id not in (None, cadence.TEMPLATE, cadence.RULES_FALLBACK)
            if gemini_text and not switched_off:
                # The AI's last wording for this same class stands; the decision itself is fresh.
                audit(self.s, "ai", "ai.explanation_kept", incident.id, why=result.fallback_reason)
            else:
                template = analyst.template_verdict(current.classification, current.confidence, score)
                current.summary, current.uncertainty_notes = template.summary, template.uncertainty_notes
                current.recommended_actions = []
                incident.verdict = current.model_dump()
                incident.model_id = cadence.TEMPLATE
                incident.prompt_version = None
                incident.ai_available = False
                audit(self.s, "ai", "ai.explanation_failed", incident.id, why=result.fallback_reason)
        await self.s.commit()
        return []

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
            if (incident.assessment_context or {}).get(REPORT_DUE):
                incident.assessment_context = {
                    k: v for k, v in incident.assessment_context.items() if k != REPORT_DUE
                }
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
        "trigger_tiers": {t: trig.TRIGGER_TIER[t] for t in i.triggers or [] if t in trig.TRIGGER_TIER},
        # AI evidence items that did not match our numbers and were dropped (the verdict on record).
        "dropped_evidence": i.dropped_evidence or 0,
        "ai_fallback_reason": i.ai_fallback_reason,
        "model_id": i.model_id,
        "verdict_at": i.verdict_at,
        # The scorecard that decided (app/detection/classifier.py) and how: scorecard alone, or a Gemini
        # tie-break that agreed or disagreed.
        "classifier_version": i.classifier_version,
        "decided_by": i.decided_by,
        "class_probs": i.class_probs,
        "class_margin": i.class_margin,
        "scorecard": i.scorecard,
    }
