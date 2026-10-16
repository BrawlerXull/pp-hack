"""Incident lifecycle: detect → judge (AI) → decide (policy) → act → explain → stand down."""

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.actions.executor import Executor
from app.ai import analyst, reporter
from app.db.audit import audit
from app.db.models import Action, ActionType, GuardState, Incident, IncidentStatus, MerchantSettings, Report
from app.detection import baseline as baseline_mod
from app.detection import features as feat
from app.detection import triggers as trig
from app.events import publish
from app.paypal.client import PayPalClient
from app.policy.engine import PlannedAction, PolicyInput, decide

log = structlog.get_logger()


def active_sale_window(ms: MerchantSettings, now: datetime) -> dict[str, Any] | None:
    for w in ms.sale_windows or []:
        try:
            start, end = datetime.fromisoformat(w["start"]), datetime.fromisoformat(w["end"])
        except (KeyError, ValueError):
            continue
        if start <= now <= end:
            return w
    return None


async def open_incident(session: AsyncSession, shop_id: str) -> Incident | None:
    q = select(Incident).where(Incident.shop_id == shop_id, Incident.status == IncidentStatus.OPEN)
    return (await session.execute(q.order_by(Incident.opened_at.desc()).limit(1))).scalar_one_or_none()


class IncidentService:
    def __init__(self, session: AsyncSession, paypal: PayPalClient | None, reassess_after: timedelta):
        self.s = session
        self.paypal = paypal
        self.reassess_after = reassess_after

    async def tick(self, shop_id: str, now: datetime | None = None) -> Incident | None:
        """One detector pass for a shop. Safe to call repeatedly."""
        now = now or datetime.now(UTC)
        ms = await self.s.get(MerchantSettings, shop_id)
        if ms is None:
            return None
        rows = await baseline_mod.recent_rows(self.s, shop_id, now - timedelta(minutes=60))
        base = await baseline_mod.load_baseline(self.s, shop_id, now)
        features = feat.compute_all(rows, now, base)
        fired = trig.evaluate(features, base)
        incident = await open_incident(self.s, shop_id)

        if incident is None:
            if not fired:
                return None
            incident = Incident(shop_id=shop_id, opened_at=now, last_signal_at=now, triggers=fired)
            self.s.add(incident)
            await self.s.flush()
            audit(self.s, "detector", "incident.opened", incident.id, triggers=fired)
            log.info("incident_opened", incident=incident.id, triggers=fired)
            await self._assess(incident, ms, rows, base, features, fired, now, first=True)
        else:
            if fired:
                incident.last_signal_at = now
                incident.triggers = sorted(set(incident.triggers or []) | set(fired))
            quiet_for = now - incident.last_signal_at
            if quiet_for >= timedelta(minutes=ms.deescalate_after_min) and trig.quiet_since(
                rows, now, ms.deescalate_after_min
            ):
                await self.close(incident, "auto_deescalate_quiet")
            elif fired and (
                incident.last_assessed_at is None or now - incident.last_assessed_at >= self.reassess_after
            ):
                await self._assess(incident, ms, rows, base, features, fired, now, first=False)
            else:
                await self._enforce(incident, ms, rows, base, now)
        await self.s.commit()
        return incident

    def _plan(
        self, incident: Incident, ms: MerchantSettings, sig: trig.Signature, now: datetime
    ) -> list[PlannedAction]:
        return decide(
            PolicyInput(
                classification=incident.classification or "unclear",
                confidence=incident.confidence or 0.0,
                autopilot=ms.autopilot,
                kill_switch=ms.kill_switch,
                sale_window_active=active_sale_window(ms, now) is not None,
                attack_fingerprints=sig.fingerprints,
                attack_sku=sig.top_sku,
            )
        )

    async def _enforce(
        self,
        incident: Incident,
        ms: MerchantSettings,
        rows: list[feat.AttemptRow],
        base: feat.Baseline,
        now: datetime,
    ) -> None:
        """Between AI assessments: re-apply the current verdict's policy to new data (no AI call).
        Catches captures that landed while the AI was deciding and bot devices seen since."""
        if incident.classification is None:
            return
        sig = trig.signature(rows, incident.opened_at - timedelta(minutes=15), now, base)
        incident.signature = sig.as_dict()
        planned = [p for p in self._plan(incident, ms, sig, now) if p.type != ActionType.NOTIFY]
        executed = await Executor(self.s, self.paypal).apply(incident, planned)
        if executed:
            audit(
                self.s, "policy", "enforced", incident.id, actions=[f"{a.type}:{a.status}" for a in executed]
            )

    async def _assess(
        self,
        incident: Incident,
        ms: MerchantSettings,
        rows: list[feat.AttemptRow],
        base: feat.Baseline,
        features: dict[int, feat.Features],
        fired: list[str],
        now: datetime,
        first: bool,
    ) -> None:
        gs = await self.s.get(GuardState, incident.shop_id)
        assert gs is not None
        sale = active_sale_window(ms, now)
        sig = trig.signature(rows, min(incident.opened_at, now) - timedelta(minutes=15), now, base)
        prior = (await self.s.execute(select(Action.type).where(Action.incident_id == incident.id))).scalars()

        analyst_input = analyst.build_input(
            features=features,
            baseline=base,
            triggers=fired,
            sale_window=sale,
            guard_state={
                "mode": gs.mode,
                "blocked_devices": len(gs.blocked_fingerprints or []),
                "held_skus": gs.held_skus,
            },
            actions_so_far=sorted(set(prior)),
            sample=[r for r in rows if r.ts > now - timedelta(minutes=15)],
        )
        result = await analyst.analyse(analyst_input, features)
        v = result.verdict

        incident.features = {f"{m}m": f.as_dict() for m, f in features.items()} | {"baseline": base.public()}
        incident.signature = sig.as_dict()
        incident.verdict = v.model_dump()
        incident.classification = v.classification
        incident.confidence = v.confidence
        incident.model_id = result.model_id
        incident.prompt_version = analyst.PROMPT_VERSION
        incident.ai_available = result.ai_available
        incident.last_assessed_at = now
        audit(
            self.s,
            "ai",
            "verdict",
            incident.id,
            classification=v.classification,
            confidence=v.confidence,
            model=result.model_id,
            dropped_evidence=result.dropped_evidence,
            first=first,
        )

        planned = self._plan(incident, ms, sig, now)
        audit(self.s, "policy", "decided", incident.id, actions=[f"{p.type}:{p.rule}" for p in planned])
        executed = await Executor(self.s, self.paypal).apply(incident, planned)
        await publish(
            self.s, "incident.updated" if not first else "incident.opened", incident_event(incident)
        )

        # Notify the owner on the first verdict and whenever new defences were switched on.
        if first or any(a.type != ActionType.REFUND for a in executed):
            await self.write_report(incident)

    async def write_report(self, incident: Incident) -> Report:
        texts, model_id = await reporter.write(self.s, incident)
        report = await self.s.get(Report, incident.id)
        if report is None:
            report = Report(incident_id=incident.id, **texts, model_id=model_id)
            self.s.add(report)
        else:
            for k, v in texts.items():
                setattr(report, k, v)
            report.model_id = model_id
        await self.s.flush()
        await publish(self.s, "alert", {"incident_id": incident.id, "text": report.alert_text})
        return report

    async def close(self, incident: Incident, rule: str) -> None:
        await Executor(self.s, self.paypal).deescalate(incident, rule)
        incident.status = IncidentStatus.CLOSED
        incident.closed_at = datetime.now(UTC)
        audit(self.s, "system", "incident.closed", incident.id, rule=rule)
        await self.write_report(incident)
        await publish(self.s, "incident.closed", incident_event(incident))
        log.info("incident_closed", incident=incident.id, rule=rule)


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
    }
