import json

import httpx
import pytest
import respx

from app.config import SANDBOX_API_BASE, Settings
from app.paypal.client import PayPalClient
from app.paypal.webhooks import Verification, build_verify_body, verify_webhook

SETTINGS = Settings(paypal_client_id="id", paypal_client_secret="secret", paypal_webhook_id="WH-1")

WEBHOOK_HEADERS = {
    "paypal-auth-algo": "SHA256withRSA",
    "paypal-cert-url": "https://api.sandbox.paypal.com/cert.pem",
    "paypal-transmission-id": "tx-1",
    "paypal-transmission-sig": "sig",
    "paypal-transmission-time": "2026-10-12T00:00:00Z",
}


def mock_token(router: respx.MockRouter) -> respx.Route:
    return router.post("/v1/oauth2/token").respond(200, json={"access_token": "tok", "expires_in": 3600})


def make_client() -> PayPalClient:
    return PayPalClient(SETTINGS, http=httpx.AsyncClient(base_url=SANDBOX_API_BASE))


def test_settings_reject_live_paypal():
    with pytest.raises(ValueError, match="sandbox"):
        Settings(paypal_api_base="https://api-m.paypal.com")


@respx.mock(base_url=SANDBOX_API_BASE)
async def test_token_cached_and_post_gets_idempotency_key(respx_mock):
    token = mock_token(respx_mock)
    orders = respx_mock.post("/v2/checkout/orders").respond(
        201, json={"id": "O1"}, headers={"paypal-debug-id": "d1"}
    )
    client = make_client()

    r1 = await client.request("POST", "/v2/checkout/orders", json={})
    await client.request("POST", "/v2/checkout/orders", json={})

    assert token.call_count == 1
    assert r1.ok and r1.json == {"id": "O1"} and r1.debug_id == "d1"
    assert orders.calls[0].request.headers["PayPal-Request-Id"]
    assert orders.calls[0].request.headers["Authorization"] == "Bearer tok"


@respx.mock(base_url=SANDBOX_API_BASE)
async def test_retries_5xx_with_same_request_id(respx_mock, monkeypatch):
    monkeypatch.setattr("app.paypal.client.asyncio.sleep", _no_sleep)
    mock_token(respx_mock)
    route = respx_mock.post("/v2/payments/captures/C1/refund").mock(
        side_effect=[httpx.Response(503), httpx.Response(201, json={"status": "COMPLETED"})]
    )
    res = await make_client().request("POST", "/v2/payments/captures/C1/refund", json={})

    assert res.status == 201
    ids = {c.request.headers["PayPal-Request-Id"] for c in route.calls}
    assert len(route.calls) == 2 and len(ids) == 1


@respx.mock(base_url=SANDBOX_API_BASE)
async def test_does_not_retry_4xx(respx_mock):
    mock_token(respx_mock)
    route = respx_mock.post("/v2/checkout/orders").respond(
        422,
        json={"name": "UNPROCESSABLE_ENTITY", "message": "m", "details": [{"issue": "INSTRUMENT_DECLINED"}]},
    )
    res = await make_client().request("POST", "/v2/checkout/orders", json={})

    assert route.call_count == 1
    assert res.error_summary() == {
        "name": "UNPROCESSABLE_ENTITY",
        "message": "m",
        "issues": ["INSTRUMENT_DECLINED"],
    }


def test_verify_body_keeps_raw_event_bytes():
    raw = '{"id":"WH-EVT","event_type":"PAYMENT.CAPTURE.COMPLETED","amount":3.10}'
    body = build_verify_body(WEBHOOK_HEADERS, "WH-1", raw)

    assert body is not None and body.endswith(f'"webhook_event": {raw}}}')
    parsed = json.loads(body)
    assert parsed["webhook_id"] == "WH-1" and parsed["transmission_id"] == "tx-1"


@respx.mock(base_url=SANDBOX_API_BASE)
async def test_verify_webhook_outcomes(respx_mock):
    mock_token(respx_mock)
    respx_mock.post("/v1/notifications/verify-webhook-signature").respond(
        200, json={"verification_status": "SUCCESS"}
    )
    client = make_client()

    assert await verify_webhook(client, WEBHOOK_HEADERS, "{}", "WH-1") == Verification.SUCCESS
    assert await verify_webhook(client, {}, "{}", "WH-1") == Verification.MISSING_HEADERS
    assert await verify_webhook(client, WEBHOOK_HEADERS, "{}", "") == Verification.NO_WEBHOOK_ID


async def _no_sleep(_: float) -> None:
    return None
