"""Live AI evaluation: does Gemini tell attacks, rushes and outages apart?

Builds features from each simulated scenario (no database, no PayPal), runs the real analyst, and
scores the verdicts. Usage: uv run python scripts/eval_ai.py [--runs 2]
"""

import asyncio
import random
import time
from datetime import UTC, datetime, timedelta

import typer

from app.ai import analyst
from app.detection.features import AttemptRow, Baseline, compute_all
from app.detection.triggers import evaluate
from app.shop.catalog import PRODUCTS
from app.shop.service import email_domain, fingerprint
from app.simulator.scenarios import BROWSER_UA, COMMON_DOMAINS, SCENARIOS, customer_pool

EXPECTED = {
    "card_testing": {"card_testing"},
    "account_testing": {"account_testing", "card_testing"},
    "slow_drip": {"card_testing"},
    "flash_sale": {"genuine_rush"},
    "paypal_outage": {"paypal_outage"},
}
OUTCOME = {"approve": "captured", "decline": "declined", "error": "error"}


def rows_for(name: str, seed: int, now: datetime) -> list[AttemptRow]:
    attempts = list(SCENARIOS[name]("offline", seed=seed))
    t = now
    rows = []
    for a in reversed(attempts):
        rows.append(
            AttemptRow(
                ts=t,
                sku=a.sku,
                amount=float(PRODUCTS[a.sku].price),
                method=a.method,
                fingerprint_hash=fingerprint(a.device_id, a.user_agent),
                email_domain=email_domain(a.email),
                card_key=(a.card_number[:6] + a.card_number[-4:]) if a.card_number else None,
                payer_id_hash=a.payer_id,
                outcome=OUTCOME[a.outcome],
                decline_code=a.decline_code
                if a.outcome == "decline"
                else ("SERVICE_UNAVAILABLE" if a.outcome == "error" else None),
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
    correct = total = 0
    for name, expected in EXPECTED.items():
        for r in range(runs):
            rows = rows_for(name, seed=r + 1, now=now)
            feats = compute_all(rows, now, base)
            fired = evaluate(feats, base)
            sale = {"note": "Autumn sale announced on Instagram"} if name == "flash_sale" else None
            inp = analyst.build_input(
                features=feats,
                baseline=base,
                triggers=fired,
                sale_window=sale,
                guard_state={"mode": "normal"},
                actions_so_far=[],
                sample=[x for x in rows if x.ts > now - timedelta(minutes=15)],
            )
            t0 = time.time()
            res = await analyst.analyse(inp, feats)
            v = res.verdict
            ok = v.classification in expected
            correct += ok
            total += 1
            print(
                f"{'PASS' if ok else 'FAIL'} {name:<16} triggers={','.join(fired) or '-':<45} "
                f"→ {v.classification:<15} "
                f"conf={v.confidence:.2f} evidence={len(v.evidence)} dropped={res.dropped_evidence} "
                f"{res.model_id} {time.time() - t0:.1f}s"
            )
            print(f"     {v.summary}")
    print(f"\nAccuracy: {correct}/{total}")


def cli(runs: int = typer.Option(1, help="Runs per scenario (different seeds)")) -> None:
    random.seed(0)
    asyncio.run(main(runs))


if __name__ == "__main__":
    typer.run(cli)
