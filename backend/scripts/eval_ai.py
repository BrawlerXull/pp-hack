"""Live AI evaluation: scorecard alone vs Gemini alone vs both (the production path).

Builds features from each simulated scenario (no database, no PayPal), runs the deterministic scorecard
and the real analyst (Gemini, as the independent second opinion), and scores three columns:
  scorecard  app/detection/classifier.py on its own (what decides in production)
  gemini     the analyst's own classification (what decided before P9)
  both       app/incidents/decision.combine: Gemini only breaks the scorecard's ties
Needs GEMINI_API_KEY and spends quota. Usage: uv run python scripts/eval_ai.py [--runs 2]
The offline, no-AI eval of the scorecard and triggers is scripts/eval_detection.py.

flash_sale_no_hint is the false-positive test: the same real-customer rush with no owner-announced sale
window, so the model has to read it from the traffic. A miss at confidence >= 0.70 would block (and above
0.85 refund) real customers, so it is scored separately and always with at least NO_HINT_MIN_SEEDS seeds.
"""

import asyncio
import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import typer

from app.ai import analyst
from app.detection import classifier
from app.detection.features import AttemptRow, Baseline, card_key, compute_all
from app.detection.triggers import evaluate
from app.incidents.decision import combine
from app.shop.catalog import PRODUCTS
from app.shop.service import classify, email_domain, fingerprint, offline_result
from app.simulator.runner import to_request
from app.simulator.scenarios import BROWSER_UA, COMMON_DOMAINS, SCENARIOS, customer_pool

EXPECTED = {
    "card_testing": {"card_testing"},
    "account_testing": {"account_testing", "card_testing"},
    "slow_drip": {"card_testing"},
    "flash_sale": {"genuine_rush"},
    "paypal_outage": {"paypal_outage"},
}


@dataclass(frozen=True)
class Case:
    scenario: str  # simulator scenario that generates the traffic
    expected: frozenset[str]
    sale_window: dict[str, str] | None = None
    min_seeds: int = 1
    separate: bool = False  # scored on its own line, not mixed into the headline accuracy


SALE = {"note": "Autumn sale announced on Instagram"}
NO_HINT_MIN_SEEDS = 3
CASES: dict[str, Case] = {
    name: Case(name, frozenset(exp), SALE if name == "flash_sale" else None) for name, exp in EXPECTED.items()
} | {
    "flash_sale_no_hint": Case(
        "flash_sale", frozenset({"genuine_rush"}), None, min_seeds=NO_HINT_MIN_SEEDS, separate=True
    ),
}


def rows_for(name: str, seed: int, now: datetime) -> list[AttemptRow]:
    attempts = list(SCENARIOS[name]("offline", seed=seed))
    t = now
    rows = []
    for a in reversed(attempts):
        # Same offline result + code table as the shop flow, so the eval sees what the DB would store.
        req = to_request("eval", name, a, "offline")
        assert req.offline is not None
        c = classify(offline_result(req.offline, False, a.method, req.card), a.method)
        rows.append(
            AttemptRow(
                ts=t,
                sku=a.sku,
                amount=float(PRODUCTS[a.sku].price),
                method=a.method,
                fingerprint_hash=fingerprint(a.device_id, a.user_agent),
                email_domain=email_domain(a.email),
                card_key=card_key(a.card_number[:6], a.card_number[-4:], a.card_expiry)
                if a.card_number
                else None,
                payer_id_hash=a.payer_id,
                outcome=c.outcome,
                decline_code=c.decline_code,
                decline_category=c.category,
            )
        )
        t -= timedelta(seconds=a.delay_s)
    return rows


def baseline() -> Baseline:
    pool = customer_pool()
    return Baseline(
        attempts_per_hour_mean=0.4,
        attempts_per_hour_std=0.7,
        decline_rate=0.03,
        error_rate=0.005,
        known_domains=frozenset(COMMON_DOMAINS),
        known_fingerprints=frozenset(fingerprint(d, BROWSER_UA) for d, _ in pool),
        history_days=28,
    )


async def main(runs: int) -> None:
    now = datetime.now(UTC)
    base = baseline()
    # separate? → approach → [correct, total]
    score = {sep: {k: [0, 0] for k in ("scorecard", "gemini", "both")} for sep in (False, True)}
    for name, case in CASES.items():
        expected = case.expected
        for r in range(max(runs, case.min_seeds)):
            rows = rows_for(case.scenario, seed=r + 1, now=now)
            feats = compute_all(rows, now, base)
            fired = evaluate(feats, base)
            sale = case.sale_window
            inp = analyst.build_input(
                features=feats,
                baseline=base,
                triggers=fired,
                sale_window=sale,
                guard_state={"mode": "normal"},
                actions_so_far=[],
                sample=[x for x in rows if x.ts > now - timedelta(minutes=15)],
            )
            card = classifier.classify(feats, base, sale_active=sale is not None)
            t0 = time.time()
            res = await analyst.analyse(inp, feats)
            v = res.verdict
            both = combine(
                card, v if res.ai_available else None, model_id=res.model_id, verified=res.verified
            )
            answers = {
                "scorecard": card.classification,
                "gemini": v.classification,
                "both": both.classification,
            }
            for k, cls in answers.items():
                score[case.separate][k][0] += cls in expected
                score[case.separate][k][1] += 1
            print(
                f"{name:<18} triggers={','.join(fired) or '-':<45} "
                + " ".join(f"{k}={c}{'' if c in expected else '(x)'}" for k, c in answers.items())
                + f" gemini_conf={v.confidence:.2f} dropped={res.dropped_evidence} {res.model_id} "
                f"{time.time() - t0:.1f}s"
            )
    for sep, title in ((False, "Accuracy"), (True, "Unannounced rush (flash_sale_no_hint)")):
        print(f"\n{title}: " + ", ".join(f"{k} {c}/{n}" for k, (c, n) in score[sep].items()))


def cli(runs: int = typer.Option(1, help="Runs per scenario (different seeds)")) -> None:
    random.seed(0)
    asyncio.run(main(runs))


if __name__ == "__main__":
    typer.run(cli)
