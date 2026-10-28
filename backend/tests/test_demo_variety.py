"""Demo runs vary from run to run (fresh seed, attacker profile, attempt count), while the canonical scenario
shapes the eval and other tests rely on stay fixed. Runs also record the defences already in place."""

from collections import Counter

from app.api import demo as demo_api
from app.db.models import GuardMode, GuardState, Outcome
from app.guard.check import invalidate_cache
from app.simulator import runner
from app.simulator.runner import SimRun
from app.simulator.scenarios import CANONICAL, account_testing, card_testing, slow_drip, stealth_bot
from tests.conftest import SHOP
from tests.test_dashboard_visibility import client  # noqa: F401 (fixture)


def shape(attempts):
    return (
        len({a.device_id for a in attempts}),
        sum(a.outcome == "approve" for a in attempts),
        tuple(sorted(Counter(a.sku for a in attempts).items())),
        round(sum(a.delay_s for a in attempts), 1),
    )


def test_canonical_card_testing_keeps_its_fixed_shape():
    attempts = list(card_testing("offline", count=120, seed=1))
    assert CANONICAL.devices == 4 and len({a.device_id for a in attempts}) == 4
    devices = [a.device_id for a in attempts]
    assert devices[:8] == devices[:4] * 2  # strict turns
    assert {a.sku for a in attempts} == {"SKU-TEALIGHT-3"}


def test_varied_attacks_differ_between_seeds_and_replay_exactly_with_the_same_seed():
    shapes = {shape(list(card_testing("offline", count=100, seed=s, vary=True))) for s in range(1, 9)}
    assert len(shapes) == 8  # no two runs alike
    a = list(card_testing("offline", count=100, seed=42, vary=True))
    b = list(card_testing("offline", count=100, seed=42, vary=True))
    assert shape(a) == shape(b) and [x.device_id for x in a] == [x.device_id for x in b]


def test_varied_attacks_are_still_bot_traffic_on_mostly_the_cheapest_item():
    for s in range(1, 21):
        attempts = list(card_testing("offline", count=100, seed=s, vary=True))
        assert all(a.is_bot for a in attempts)
        assert sum(a.sku == "SKU-TEALIGHT-3" for a in attempts) >= 75
        assert 2 <= len({a.device_id for a in attempts}) <= 50


def test_other_attack_scenarios_accept_vary():
    for fn in (account_testing, slow_drip, stealth_bot):
        one = list(fn("offline", count=20, seed=3, vary=True))
        two = list(fn("offline", count=20, seed=4, vary=True))
        assert len(one) == 20 and shape(one) != shape(two)


def test_start_run_picks_a_seed_and_jitters_the_count(monkeypatch):
    monkeypatch.setattr(runner, "RUNS", {})
    monkeypatch.setattr(runner.asyncio, "create_task", lambda coro: (coro.close(), _Done())[1])
    totals, seeds = set(), set()
    for _ in range(12):
        r = runner.start_run(SHOP, "card_testing", "offline", None, context={"defences_active": False})
        seeds.add(r.seed)
        totals.add(r.total)
        assert 84 <= r.total <= 156  # default 120 +/- 30%
    assert len(seeds) == 12 and len(totals) > 3
    fixed = runner.start_run(SHOP, "card_testing", "offline", None, count=50, seed=7)
    again = runner.start_run(SHOP, "card_testing", "offline", None, count=50, seed=7)
    assert fixed.total == 50 and fixed.seed == 7 and again.total == 50


def test_run_counts_what_reached_paypal():
    r = SimRun(id="x", scenario="card_testing", mode="live", total=10, done=10)
    r.outcomes.update({Outcome.BLOCKED: 6, Outcome.CHALLENGED: 3, Outcome.DECLINED: 1})
    assert r.stopped_by_guard == 9 and r.reached_paypal == 1


async def test_a_run_records_the_defences_already_in_place(db, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(runner, "RUNS", {})
    monkeypatch.setattr(demo_api, "RUNS", runner.RUNS)
    captured = {}

    def fake_start(*args, **kwargs):
        captured["context"] = args[7]
        run = SimRun(id="r1", scenario=args[1], mode=args[2], total=5, seed=1, context=args[7])
        run.finished_at = run.started_at  # done at once, so the next run isn't refused with 409
        runner.RUNS[run.id] = run
        return run

    monkeypatch.setattr(demo_api, "start_run", fake_start)
    res = await client.post("/demo/run/card_testing", json={"mode": "offline"})
    assert res.status_code == 200 and res.json()["context"]["defences_active"] is False

    gs = await db.get(GuardState, SHOP)
    gs.mode, gs.held_skus = GuardMode.SCA_ALL, ["SKU-TEALIGHT-3"]
    await db.commit()
    invalidate_cache(SHOP)
    runner.RUNS.clear()
    res = await client.post("/demo/run/card_testing", json={"mode": "offline"})
    ctx = res.json()["context"]
    assert ctx["defences_active"] and ctx["sca_all"] and ctx["held_skus"] == ["SKU-TEALIGHT-3"]

    listed = (await client.get("/demo/runs")).json()[0]
    assert {"seed", "context", "stopped_by_guard", "reached_paypal"} <= set(listed)


class _Done:
    def add_done_callback(self, _cb):
        return None
