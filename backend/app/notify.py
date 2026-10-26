"""Optional push of owner alerts to a chat webhook (NOTIFY_WEBHOOK_URL): Slack-style incoming webhooks
get {"text": ...}; an ntfy topic URL (https://ntfy.sh/<topic>) gets ntfy's JSON publish format.

Best effort by design: the alert is already on record (audit `alert.sent`) and on the dashboard before this
runs, so a slow or failing webhook is logged and dropped. It runs as a background task with a short timeout,
never inside the detector tick or a database transaction.
"""

import asyncio
from typing import Any
from urllib.parse import urlsplit

import httpx
import structlog

from app.config import get_settings

log = structlog.get_logger()
TITLE = "Checkout Guard"
_tasks: set[asyncio.Task[None]] = set()  # strong refs: a bare create_task may be garbage-collected


def request_for(url: str, text: str, incident_id: str) -> tuple[str, dict[str, Any]]:
    """(target URL, JSON body). ntfy only reads JSON posted to its root, with the topic in the body."""
    parts = urlsplit(url)
    topic = parts.path.strip("/")
    if parts.hostname and parts.hostname.startswith("ntfy.") and topic and "/" not in topic:
        return f"{parts.scheme}://{parts.netloc}/", {"topic": topic, "title": TITLE, "message": text}
    return url, {"text": f"{TITLE}: {text}", "incident_id": incident_id}


async def _post(url: str, text: str, incident_id: str, timeout_s: float) -> None:
    target, body = request_for(url, text, incident_id)
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            res = await client.post(target, json=body)
        if res.status_code >= 400:
            log.warning("notify_webhook_rejected", status=res.status_code, incident=incident_id)
    except Exception as exc:  # best effort: never let a webhook problem reach the caller
        log.warning("notify_webhook_failed", error=exc.__class__.__name__, incident=incident_id)


def send(text: str, incident_id: str) -> None:
    """Schedule the post and return at once. No-op when NOTIFY_WEBHOOK_URL is unset. Call after commit."""
    settings = get_settings()
    if not settings.notify_webhook_url or not text:
        return
    task = asyncio.create_task(
        _post(settings.notify_webhook_url, text, incident_id, settings.notify_timeout_s)
    )
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def drain() -> None:
    """Wait for posts still in progress (tests; a clean shutdown)."""
    if _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)
