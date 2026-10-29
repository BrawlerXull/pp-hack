"""Push notifications to members' browsers and installed apps, through Firebase Cloud Messaging (HTTP v1).

Best effort, like app/notify.py: every alert is already on record and on the dashboard before a push goes
out, so a slow or failing FCM call is logged and dropped. Code that is inside a transaction calls queue();
the pushes go out only after that transaction commits (a rolled-back refund never buzzes a phone). Code
that has already committed calls send().

Messages are data-only: the service worker (frontend/public/sw.js) draws the notification itself, the same
way in every browser, and opens `url` when it is tapped.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import structlog
from sqlalchemy import delete, select
from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import PUSH_KINDS, PushDevice, User
from app.db.session import get_sessionmaker

log = structlog.get_logger()
SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
TIMEOUT_S = 5.0
_tasks: set[asyncio.Task[Any]] = set()
_creds: Any = None  # google.oauth2.service_account.Credentials, refreshed when expired


def service_account() -> dict[str, Any] | None:
    """FIREBASE_SERVICE_ACCOUNT: the JSON itself, or a path to the downloaded file."""
    raw = get_settings().firebase_service_account.strip()
    if not raw:
        return None
    try:
        return json.loads(raw if raw.startswith("{") else Path(raw).expanduser().read_text())
    except (OSError, ValueError):
        log.warning("push_bad_service_account")
        return None


def web_config() -> dict[str, Any] | None:
    raw = get_settings().firebase_web_config.strip()
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        log.warning("push_bad_web_config")
        return None


def configured() -> bool:
    return service_account() is not None


async def _access_token(info: dict[str, Any]) -> str:
    global _creds
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account as sa

    if _creds is None or _creds.service_account_email != info.get("client_email"):
        _creds = sa.Credentials.from_service_account_info(info, scopes=[SCOPE])
    if not _creds.valid:
        await asyncio.to_thread(_creds.refresh, Request())
    return _creds.token


def message(token: str, kind: str, title: str, body: str, url: str, tag: str) -> dict[str, Any]:
    data = {"kind": kind, "title": title, "body": body, "url": url, "tag": tag}
    return {
        "message": {
            "token": token,
            "data": data,
            # High urgency: an attack alert should wake the phone, not wait for the next batch.
            "webpush": {"headers": {"Urgency": "high", "TTL": "3600"}},
        }
    }


async def deliver(
    kind: str, title: str, body: str, url: str, tag: str, user_ids: list[str] | None = None
) -> dict[str, int]:
    """Send now to every device whose member wants this kind (or to `user_ids`' devices, for the test
    button). Tokens FCM says are gone are deleted. Returns counts; never raises."""
    info = service_account()
    if info is None:
        return {"sent": 0, "failed": 0}
    sent = failed = 0
    try:
        async with get_sessionmaker()() as s:
            q = select(PushDevice.token, User.push_kinds).join(User, User.id == PushDevice.user_id)
            if user_ids is not None:
                q = q.where(PushDevice.user_id.in_(user_ids))
            rows = (await s.execute(q)).all()
            tokens = [t for t, kinds in rows if user_ids is not None or kind in (kinds or PUSH_KINDS)]
            if not tokens:
                return {"sent": 0, "failed": 0}
            access = await _access_token(info)
            endpoint = f"https://fcm.googleapis.com/v1/projects/{info['project_id']}/messages:send"
            gone: list[str] = []
            async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
                for token in tokens:
                    res = await client.post(
                        endpoint,
                        json=message(token, kind, title, body, url, tag),
                        headers={"Authorization": f"Bearer {access}"},
                    )
                    if res.status_code == 200:
                        sent += 1
                        continue
                    failed += 1
                    status = (res.json().get("error") or {}).get("status") if res.content else None
                    if res.status_code == 404 or status in {"NOT_FOUND", "UNREGISTERED", "INVALID_ARGUMENT"}:
                        gone.append(token)  # uninstalled, permission revoked, or a stale token
                    else:
                        log.warning("push_rejected", status=res.status_code, kind=kind)
            if gone:
                await s.execute(delete(PushDevice).where(PushDevice.token.in_(gone)))
                await s.commit()
    except Exception as exc:  # best effort: never let a push problem reach the caller
        log.warning("push_failed", error=exc.__class__.__name__, kind=kind)
    log.info("push_sent", kind=kind, sent=sent, failed=failed)
    return {"sent": sent, "failed": failed}


def send(kind: str, title: str, body: str, url: str, tag: str) -> None:
    """Schedule a push and return at once. No-op without a service account. Call after commit."""
    if not configured():
        return
    task = asyncio.create_task(deliver(kind, title, body, url, tag))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def queue(session: AsyncSession, kind: str, title: str, body: str, url: str, tag: str) -> None:
    """Send once `session` commits; dropped on rollback. Pushes with the same tag in one transaction merge
    into one, and "{n}" in the title becomes how many were queued (twelve held payments: one push)."""
    pending: dict[str, dict[str, Any]] = session.sync_session.info.setdefault("push", {})
    if tag in pending:
        pending[tag]["n"] += 1
    else:
        pending[tag] = {"kind": kind, "title": title, "body": body, "url": url, "tag": tag, "n": 1}


@sa_event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    for p in session.info.pop("push", {}).values():
        n = p.pop("n")
        p["title"] = p["title"].format(n=n, s="" if n == 1 else "s")
        send(**p)


@sa_event.listens_for(Session, "after_rollback")
def _after_rollback(session: Session) -> None:
    session.info.pop("push", None)


async def drain() -> None:
    """Wait for pushes still in progress (tests; a clean shutdown)."""
    if _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)
