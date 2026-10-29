"""Thin async PayPal REST client.

- OAuth client-credentials token, cached and refreshed 60 s before expiry (the demo must
  keep running unattended until judging ends on Dec 15, 2026).
- `PayPal-Request-Id` idempotency key on every POST. Callers pass a key derived from our own ids
  (cg-order-{attempt}, cg-capture-{order}, cg-refund-{attempt}), so a repeat of the same business
  operation is the same request to PayPal; a random key is only the fallback.
- Retries 429/5xx with backoff; never retries 4xx.
- Never raises for PayPal trouble: network and OAuth failures come back as a status-0 response, which
  callers already treat as a processor error (an outage signal, not a crash of the detector tick).
- Returns the `paypal-debug-id` so callers can store it next to each row.
"""

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings

RETRY_STATUSES = {429, 500, 502, 503, 504}


class PayPalAuthError(RuntimeError):
    pass


@dataclass
class PayPalResponse:
    status: int
    json: Any
    debug_id: str | None
    elapsed_ms: int

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def error_summary(self) -> dict[str, Any] | None:
        if self.ok:
            return None
        if not isinstance(self.json, dict):
            return {"name": f"HTTP_{self.status}", "message": None, "issues": []}
        return {
            "name": self.json.get("name"),
            "message": self.json.get("message"),
            "issues": [d.get("issue") for d in self.json.get("details", [])],
        }


class PayPalClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None, max_retries: int = 3):
        self._settings = settings
        self._http = http or httpx.AsyncClient(base_url=settings.paypal_api_base, timeout=30)
        self._max_retries = max_retries
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    @property
    def settings(self) -> Settings:
        return self._settings

    async def aclose(self) -> None:
        await self._http.aclose()

    async def raw_post_form(self, path: str, *, data: dict[str, str], auth: tuple[str, str]) -> Any:
        res = await self._http.post(path, data=data, auth=auth)
        return res.json() if res.status_code == 200 else None

    async def access_token(self) -> str:
        async with self._token_lock:
            if self._token and time.monotonic() < self._token_expires_at - 60:
                return self._token
            if not self._settings.paypal_client_id or not self._settings.paypal_client_secret:
                raise PayPalAuthError("PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET not set")
            try:
                res = await self._http.post(
                    "/v1/oauth2/token",
                    auth=(self._settings.paypal_client_id, self._settings.paypal_client_secret),
                    data={"grant_type": "client_credentials"},
                )
                if res.status_code != 200:
                    raise PayPalAuthError(f"PayPal OAuth failed: {res.status_code} {res.text[:200]}")
                body = res.json()
                self._token = body["access_token"]
                self._token_expires_at = time.monotonic() + body["expires_in"]
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                raise PayPalAuthError(f"PayPal OAuth failed: {type(exc).__name__}: {exc}") from exc
            return self._token

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        content: str | None = None,
        request_id: str | None = None,
        files: dict[str, Any] | None = None,
    ) -> PayPalResponse:
        """`files`: a multipart/form-data body (Disputes API provide-evidence); httpx sets its boundary."""
        req_headers = {"Content-Type": "application/json", **(headers or {})}
        if files is not None:
            req_headers.pop("Content-Type")
        if method.upper() == "POST":
            # Same id across retries, so PayPal treats a retried POST as the same request.
            req_headers["PayPal-Request-Id"] = (
                request_id or req_headers.get("PayPal-Request-Id") or str(uuid.uuid4())
            )

        attempt = 0
        while True:
            try:
                token = await self.access_token()
            except PayPalAuthError as exc:
                return PayPalResponse(0, {"name": "AUTH_ERROR", "message": str(exc)}, None, 0)
            started = time.monotonic()
            try:
                res = await self._http.request(
                    method,
                    path,
                    json=json,
                    params=params,
                    content=content,
                    files=files,
                    headers={**req_headers, "Authorization": f"Bearer {token}"},
                )
            except httpx.TransportError as exc:
                # Network failure / timeout: retry, then report as status 0 (an outage signal, not a crash).
                if attempt < self._max_retries:
                    attempt += 1
                    await asyncio.sleep(0.5 * 2**attempt)
                    continue
                elapsed_ms = int((time.monotonic() - started) * 1000)
                return PayPalResponse(0, {"name": "NETWORK_ERROR", "message": str(exc)}, None, elapsed_ms)
            elapsed_ms = int((time.monotonic() - started) * 1000)

            if res.status_code == 401 and attempt == 0:
                self._token = None  # token revoked/expired early: refresh once
                attempt += 1
                continue
            if res.status_code in RETRY_STATUSES and attempt < self._max_retries:
                attempt += 1
                await asyncio.sleep(0.5 * 2**attempt)
                continue

            try:
                body = res.json() if res.content else None
            except ValueError:
                body = {"raw": res.text}
            return PayPalResponse(res.status_code, body, res.headers.get("paypal-debug-id"), elapsed_ms)
