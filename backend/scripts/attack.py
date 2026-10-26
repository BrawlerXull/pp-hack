"""Attacker terminal: start a labelled simulation and watch every attempt land, one line each.

Made for a split-screen demo video: this terminal on one side, the War Room on the other. It only drives
the demo controls (/demo/run/{scenario}) as the owner, so it is the same labelled simulator as the
Demo lab button: offline attempts are synthesised (no PayPal call), live ones are real sandbox orders with
sandbox test cards. It never talks to PayPal itself.

    uv run python scripts/attack.py card_testing --count 120 --speed 3
    uv run python scripts/attack.py flash_sale --url https://<frontend-host>/api --password <judge password>

flash_sale announces a 2-hour sale window first, like the War Room's "Simulate flash sale" button
(--no-announce-sale runs it unannounced, which the guard may treat more strictly).

Ctrl-C stops the run on the server too. Standalone on purpose (httpx + typer, no app imports), so it can
point at a deployed demo from any machine.
"""

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
import typer

POLL_S = 0.5
SUMMARY_EVERY_S = 1.0
PENDING = "created"  # a wallet order waiting for approval: printed only if the run ends with it so

OUTCOME_COLOUR = {
    "captured": typer.colors.GREEN,
    "refunded": typer.colors.BLUE,
    "declined": typer.colors.RED,
    "error": typer.colors.YELLOW,
    "blocked": typer.colors.MAGENTA,
    "challenged": typer.colors.YELLOW,
    "sca_required": typer.colors.CYAN,
}
STATUS_TEXT = {
    "quiet": "quiet",
    "investigating": "incident open, AI judging",
    "under_attack": "ATTACK confirmed",
    "busy_genuine_rush": "genuine rush (no friction)",
    "paypal_issue": "PayPal-side trouble",
    "recovering": "standing down",
}

app = typer.Typer(add_completion=False, help=__doc__.split("\n\n")[0])


def attempt_line(a: dict[str, Any]) -> str:
    """One fixed-width line per attempt. The fingerprint is the 8-character prefix the dashboard shows."""
    ts = str(a.get("ts", ""))
    clock = ts[11:19] if len(ts) >= 19 else ts
    outcome = str(a.get("outcome") or "?")
    why = a.get("decline_code") or a.get("guard_reason") or ""
    origin = "LIVE sandbox" if a.get("paypal_live") else "simulated"
    text = (
        f"{clock}  {str(a.get('method') or ''):<6} {str(a.get('sku') or ''):<16}"
        f" ${str(a.get('amount') or ''):>7}"
        f"  fp {str(a.get('fingerprint') or ''):<8}  {str(a.get('email_domain') or '-'):<22}"
        f"  {outcome.upper():<12} {why}"
    )
    return typer.style(text, fg=OUTCOME_COLOUR.get(outcome)) + typer.style(f"  [{origin}]", dim=True)


def guard_changes(before: dict[str, Any] | None, after: dict[str, Any]) -> list[str]:
    """What the defender did since the last summary, as banner lines for the attacker's screen."""
    lines = []
    b_status = (before or {}).get("status")
    if after.get("status") != b_status:
        lines.append(f"status: {STATUS_TEXT.get(after.get('status', ''), after.get('status'))}")
    b_gs, a_gs = (before or {}).get("guard_state") or {}, after.get("guard_state") or {}
    if a_gs.get("mode") != b_gs.get("mode"):
        lines.append("3-D Secure ON for card payments" if a_gs.get("mode") == "sca_all" else "3-D Secure off")
    if (n := a_gs.get("blocked_devices", 0)) != b_gs.get("blocked_devices", 0):
        lines.append(f"{n} device(s) blocked")
    if (held := a_gs.get("held_skus") or []) != (b_gs.get("held_skus") or []):
        lines.append(f"held for first-time shoppers: {', '.join(held) or 'none'}")
    b_ref = ((before or {}).get("last_24h") or {}).get("refunded", 0)
    if (r := (after.get("last_24h") or {}).get("refunded", 0)) != b_ref:
        lines.append(f"{r} payment(s) refunded (24 h)")
    return lines


@dataclass
class Follower:
    """Prints attempts of one run in time order, each once, when its outcome is known."""

    scenario: str
    since: str  # the run's started_at (ISO); earlier attempts belong to other runs
    seen: set[str] = field(default_factory=set)

    def new(self, rows: list[dict[str, Any]], final: bool = False) -> list[dict[str, Any]]:
        fresh = [
            a
            for a in rows
            if a.get("scenario") == self.scenario
            and str(a.get("ts", "")) >= self.since[:19]
            and a.get("id") not in self.seen
            and (final or a.get("outcome") != PENDING)
        ]
        fresh.sort(key=lambda a: str(a.get("ts", "")))
        self.seen.update(a["id"] for a in fresh)
        return fresh


def login(client: httpx.Client, password: str) -> None:
    res = client.post("/auth/login", json={"password": password})
    if res.status_code == 401:
        raise typer.BadParameter("wrong password (ADMIN_PASSWORD on the server)", param_hint="--password")
    res.raise_for_status()
    client.headers["Authorization"] = f"Bearer {res.json()['token']}"


class Lost(Exception):
    """The server no longer knows the run (runs live in the API's memory) or stopped answering."""


def get_json(client: httpx.Client, path: str, **params: Any) -> Any:
    res = client.get(path, params=params or None)
    if res.status_code >= 400:
        raise Lost(f"GET {path} failed with {res.status_code}")
    return res.json()


def find_run(client: httpx.Client, run_id: str) -> dict[str, Any]:
    run = next((r for r in get_json(client, "/demo/runs") if r.get("run_id") == run_id), None)
    if run is None:
        raise Lost("run no longer known to the server (API restarted?)")
    return run


def follow(
    client: httpx.Client,
    run_id: str,
    scenario: str,
    baseline: dict[str, Any] | None = None,
    sleep=time.sleep,
) -> dict[str, Any]:
    """Tail the run until the server says it finished. Returns the run's final record. `baseline` is the
    summary from before the run, so leftovers of earlier rehearsals are not shown as this run's defence."""
    run = find_run(client, run_id)
    follower = Follower(scenario, str(run["started_at"]))
    summary = baseline
    next_summary = 0.0
    while True:
        finished = run.get("finished_at") is not None
        rows = get_json(client, "/dashboard/attempts", minutes=60, limit=1000)
        for a in follower.new(rows, final=finished):
            typer.echo(attempt_line(a))
        if time.monotonic() >= next_summary or finished:
            now = get_json(client, "/dashboard/summary")
            for line in guard_changes(summary, now):
                typer.secho(f"  >> GUARD: {line}", bold=True)
            summary, next_summary = now, time.monotonic() + SUMMARY_EVERY_S
        if finished:
            return run
        sleep(POLL_S)
        run = find_run(client, run_id)


@app.command()
def attack(
    scenario: str = typer.Argument(
        "card_testing",
        help="card_testing, account_testing, slow_drip, flash_sale, paypal_outage, normal_night",
    ),
    mode: str = typer.Option("offline", help="offline (synthesised, no PayPal) or live (sandbox orders)"),
    count: int | None = typer.Option(None, min=1, max=500, help="attempts (scenario default if omitted)"),
    speed: float = typer.Option(1.0, min=0.01, max=20, help="time compression; 3 = three times faster"),
    announce_sale: bool | None = typer.Option(
        None, help="announce a 2-hour sale window first (default: on for flash_sale, like the War Room)"
    ),
    url: str = typer.Option("http://localhost:8000", help="backend URL, or <frontend>/api for a deployment"),
    password: str = typer.Option("guard-demo", envvar="ADMIN_PASSWORD", help="the dashboard password"),
) -> None:
    with httpx.Client(base_url=url.rstrip("/"), timeout=30) as client:
        login(client, password)
        baseline = get_json(client, "/dashboard/summary")
        typer.secho(
            f"guard before the run: {STATUS_TEXT.get(baseline.get('status'), baseline.get('status'))}"
        )
        announce = scenario == "flash_sale" if announce_sale is None else announce_sale
        body = {"mode": mode, "count": count, "speed": speed, "announce_sale": announce}
        res = client.post(f"/demo/run/{scenario}", json=body)
        if res.status_code >= 400:
            detail = res.json().get("detail") if "json" in res.headers.get("content-type", "") else res.text
            typer.secho(f"run refused ({res.status_code}): {detail}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
        run = res.json()
        typer.secho(
            f"SIMULATION ({run['mode']}) {run['scenario']}: {run['total']} attempts, run {run['run_id']}",
            bold=True,
        )
        started = datetime.now()
        try:
            final = follow(client, run["run_id"], scenario, baseline)
        except Lost as exc:
            typer.secho(f"lost the run: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from None
        except KeyboardInterrupt:
            client.post(f"/demo/runs/{run['run_id']}/stop")
            typer.secho("\nstopped", fg=typer.colors.YELLOW)
            raise typer.Exit(130) from None
        took = (datetime.now() - started).total_seconds()
        outcomes = ", ".join(f"{k} {v}" for k, v in sorted(final.get("outcomes", {}).items()))
        typer.secho(f"done in {took:.0f}s: {final.get('done')}/{final.get('total')} ({outcomes})", bold=True)
        if final.get("error"):
            typer.secho(f"run error: {final['error']}", fg=typer.colors.RED)


if __name__ == "__main__":
    app()
