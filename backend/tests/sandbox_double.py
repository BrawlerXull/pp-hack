"""Stateful respx stand-in for the PayPal sandbox, for tests of the live path only (never used at runtime;
the app's offline mode stays a labelled SIM, not a fake PayPal).

Response bodies follow PayPal's documented Orders v2 / Payments v2 shapes. The PAYEE_NOT_ENABLED error is
the one recorded in spike-output/spike-20261016T163103Z.jsonl. Which decline shape the sandbox really
uses for CCREJECT-* cards is still open (docs/sandbox-findings.md), so both are supported:
  decline_shape="capture"    → 201, order COMPLETED, capture DECLINED with processor_response code
  decline_shape="create_422" → 422 INSTRUMENT_DECLINED on create (issue name only, no processor code)
Repeated PayPal-Request-Ids replay the first response, as PayPal's idempotency does.
Browser card orders (Card Fields) are created without card data and stay CREATED until the test calls
card_fields_submit(), which stands in for PayPal's hosted fields and 3-D Secure window in the browser.
"""

import itertools
import json
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
import respx

from app.config import SANDBOX_API_BASE

# Copied from developer.paypal.com/api/rest/sandbox/card-testing/ on purpose, not imported from
# app/paypal/codes.py: the double must be an independent check of our trigger → code table.
SANDBOX_REJECTION_CODES = {
    "CCREJECT-REFUSED": "0500",
    "CCREJECT-SF": "9500",
    "CCREJECT-EC": "5400",
    "CCREJECT-IRC": "5180",
    "CCREJECT-IF": "5120",
    "CCREJECT-LS": "9520",
    "CCREJECT-IA": "1330",
    "CCREJECT-BANK_ERROR": "5100",
    "CCREJECT-CVV_F": "00N7",
}


def trigger_code(card_name: str | None) -> str | None:
    for trigger in sorted(SANDBOX_REJECTION_CODES, key=len, reverse=True):
        if card_name and trigger in card_name:
            return SANDBOX_REJECTION_CODES[trigger]
    return None


UNPROCESSABLE = (
    "The requested action could not be performed, semantically incorrect, or failed business validation."
)
ISSUE_TEXT = {
    "INSTRUMENT_DECLINED": "The instrument presented  was either declined by the processor or bank, "
    "or it can't be used for this payment.",
    "PAYER_ACTION_REQUIRED": "Transaction cannot complete successfully, "
    "instruct the buyer to return to PayPal.",
    "ORDER_ALREADY_CAPTURED": "Order already captured.If 'intent=CAPTURE' "
    "only one capture per order is allowed.",
    "ORDER_NOT_APPROVED": "Payer has not yet approved the Order for payment.",
    "PAYEE_NOT_ENABLED_FOR_CARD_PROCESSING": "Payee account is not setup to be able to "
    "process card payments.",
    "CAPTURE_FULLY_REFUNDED": "The capture has already been fully refunded",
}


def error_body(issue: str, debug_id: str) -> dict[str, Any]:
    return {
        "name": "UNPROCESSABLE_ENTITY",
        "details": [{"issue": issue, "description": ISSUE_TEXT.get(issue, issue)}],
        "message": UNPROCESSABLE,
        "debug_id": debug_id,
        "links": [
            {
                "href": f"https://developer.paypal.com/docs/api/orders/v2/#error-{issue}",
                "rel": "information_link",
                "method": "GET",
            }
        ],
    }


@dataclass
class _Order:
    id: str
    status: str
    unit: dict[str, Any]
    card: dict[str, Any] | None
    capture: dict[str, Any] | None = None
    intent: str = "CAPTURE"
    authorization: dict[str, Any] | None = None  # intent AUTHORIZE: the payment hold, once authorised


@dataclass
class SandboxDouble:
    decline_shape: Literal["capture", "create_422"] = "capture"
    card_processing_enabled: bool = True
    token_fails: bool = False
    # Refund trouble, counted down per request: an outage (nothing done, 503) or a lost answer (the refund
    # IS made and cached under its request id, but the caller gets a 503, as after a timeout).
    refund_outage: int = 0
    refund_answer_lost: int = 0
    valid_signature: str = "valid-sig"
    orders: dict[str, _Order] = field(default_factory=dict)
    refunds: dict[str, dict[str, Any]] = field(default_factory=dict)  # capture id → refund
    authorizations: dict[str, "_Order"] = field(default_factory=dict)  # authorization id → its order
    dispute_answers: list[tuple[str, str, bytes]] = field(default_factory=list)  # (dispute id, verb, body)
    calls: list[tuple[str, str, str | None]] = field(default_factory=list)  # (method, path, request id)
    bodies: list[bytes] = field(default_factory=list)  # every request body our server sent
    verify_bodies: list[dict[str, Any]] = field(default_factory=list)
    _replay: dict[str, tuple[int, bytes, dict[str, str]]] = field(default_factory=dict)
    client: Any = None  # PayPalClient wired to this double by the `sandbox` fixture
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))

    # ---- helpers for tests ------------------------------------------------------------------------

    def _id(self, prefix: str) -> str:
        return f"{prefix}{next(self._ids):06d}X"

    def approve(self, order_id: str) -> None:
        """The buyer approved the wallet order in PayPal's popup."""
        self.orders[order_id].status = "APPROVED"

    def complete_3ds(self, order_id: str) -> None:
        """The buyer finished the 3-D Secure page (payer-action link); the order can now be captured."""
        self.orders[order_id].status = "APPROVED"

    def card_fields_submit(
        self,
        order_id: str,
        *,
        number: str = "4868719166101368",
        name: str = "Card Holder",
        liability_shift: str | None = "POSSIBLE",
        enrollment: str | None = "Y",
        auth_status: str | None = "Y",
        bin_details: bool = True,
        status: str = "APPROVED",
    ) -> None:
        """The buyer typed a card into PayPal's hosted fields and the browser submitted it (with the bank's
        3-D Secure window when it ran). The number goes browser → PayPal: our server never sees it.
        liability_shift=None: 3-D Secure did not run (no authentication_result)."""
        o = self.orders[order_id]
        card = dict(o.card or {}) | {"number": number, "expiry": "2030-11", "name": name}
        if bin_details:
            card["_bin"] = number[:6]
        if liability_shift or enrollment or auth_status:
            tds = {
                k: v
                for k, v in (("enrollment_status", enrollment), ("authentication_status", auth_status))
                if v
            }
            card["_auth"] = {"liability_shift": liability_shift, "three_d_secure": tds}
        o.card = card
        o.status = status

    def capture_out_of_band(self, order_id: str) -> None:
        """A capture PayPal performed whose response we never saw (timeout, crashed tab)."""
        self._capture(self.orders[order_id])

    def posts(self, prefix: str) -> list[tuple[str, str, str | None]]:
        return [c for c in self.calls if c[0] == "POST" and c[1].startswith(prefix)]

    # ---- PayPal behaviour -------------------------------------------------------------------------

    def _order_json(self, o: _Order) -> dict[str, Any]:
        unit = dict(o.unit)
        if o.capture:
            unit["payments"] = {"captures": [o.capture]}
        if o.authorization:
            unit["payments"] = {"authorizations": [o.authorization]}
        body: dict[str, Any] = {
            "id": o.id,
            "intent": o.intent,
            "status": o.status,
            "purchase_units": [unit],
            "create_time": "2026-10-18T10:00:00Z",
            "links": [
                {"href": f"{SANDBOX_API_BASE}/v2/checkout/orders/{o.id}", "rel": "self", "method": "GET"}
            ],
        }
        if o.card and o.card.get("number"):
            card: dict[str, Any] = {
                "name": o.card.get("name"),
                "last_digits": str(o.card["number"])[-4:],
                "expiry": o.card.get("expiry"),
                "brand": "VISA",
                "type": "CREDIT",
            }
            if o.card.get("_bin"):
                card["bin_details"] = {
                    "bin": o.card["_bin"],
                    "issuing_bank": "Test Bank",
                    "bin_country_code": "US",
                }
            if o.card.get("_auth"):
                card["authentication_result"] = o.card["_auth"]
            body["payment_source"] = {"card": card}
        if o.status == "PAYER_ACTION_REQUIRED":
            body["links"].append(
                {
                    "href": f"https://www.sandbox.paypal.com/webapps/helios?action=verify&flow=3ds&cart_id={o.id}",
                    "rel": "payer-action",
                    "method": "GET",
                }
            )
        elif o.status == "CREATED":
            body["links"].append(
                {
                    "href": f"https://www.sandbox.paypal.com/checkoutnow?token={o.id}",
                    "rel": "approve",
                    "method": "GET",
                }
            )
        return body

    def _capture(self, o: _Order) -> None:
        code = trigger_code((o.card or {}).get("name"))
        cap_id = self._id("CAP")
        o.status = "COMPLETED"
        o.capture = {
            "id": cap_id,
            "status": "DECLINED" if code else "COMPLETED",
            "amount": o.unit["amount"],
            "final_capture": True,
            "custom_id": o.unit.get("custom_id"),
            "seller_protection": {"status": "NOT_ELIGIBLE"},
            "processor_response": {"avs_code": "Y", "cvv_code": "M", "response_code": code or "0000"},
            "links": [
                {"href": f"{SANDBOX_API_BASE}/v2/payments/captures/{cap_id}", "rel": "self", "method": "GET"},
                {
                    "href": f"{SANDBOX_API_BASE}/v2/payments/captures/{cap_id}/refund",
                    "rel": "refund",
                    "method": "POST",
                },
                {"href": f"{SANDBOX_API_BASE}/v2/checkout/orders/{o.id}", "rel": "up", "method": "GET"},
            ],
            "create_time": "2026-10-18T10:00:01Z",
        }

    def _err(self, status: int, issue: str) -> httpx.Response:
        return httpx.Response(
            status, json=error_body(issue, self._id("dbg")), headers={"paypal-debug-id": "dbg"}
        )

    def _idempotent(self, request: httpx.Request, handler: Any, **kw: Any) -> httpx.Response:
        rid = request.headers.get("PayPal-Request-Id")
        self.calls.append((request.method, request.url.path, rid))
        self.bodies.append(request.content)
        if rid and rid in self._replay:
            status, content, headers = self._replay[rid]
            return httpx.Response(status, content=content, headers=headers)
        res = handler(request, **kw)
        if rid:
            self._replay[rid] = (res.status_code, res.read(), dict(res.headers))
        return res

    def _token(self, request: httpx.Request) -> httpx.Response:
        if self.token_fails:
            return httpx.Response(
                401, json={"error": "invalid_client", "error_description": "Client Authentication failed"}
            )
        return httpx.Response(
            200, json={"access_token": "A21AA-test", "token_type": "Bearer", "expires_in": 32400}
        )

    def _create(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        unit = payload["purchase_units"][0]
        card = (payload.get("payment_source") or {}).get("card")
        if card is not None and not self.card_processing_enabled:
            return self._err(422, "PAYEE_NOT_ENABLED_FOR_CARD_PROCESSING")
        if card is not None and trigger_code(card.get("name")) and self.decline_shape == "create_422":
            return self._err(422, "INSTRUMENT_DECLINED")
        sca = ((card or {}).get("attributes") or {}).get("verification", {}).get("method") == "SCA_ALWAYS"
        if card is None or "number" not in card:
            # Wallet, or a Card Fields order: the buyer still has to approve / submit the card in the browser.
            status, sca = "CREATED", False
        else:
            status = "PAYER_ACTION_REQUIRED" if sca else "APPROVED"
        order = _Order(self._id("ORD"), status, dict(unit), card, intent=payload.get("intent", "CAPTURE"))
        self.orders[order.id] = order
        return httpx.Response(200 if sca else 201, json=self._order_json(order))

    def _capture_route(self, request: httpx.Request, oid: str) -> httpx.Response:
        o = self.orders.get(oid)
        if o is None:
            return httpx.Response(
                404, json={"name": "RESOURCE_NOT_FOUND", "details": [{"issue": "INVALID_RESOURCE_ID"}]}
            )
        if o.intent != "CAPTURE":
            return self._err(422, "ACTION_DOES_NOT_MATCH_INTENT")
        if o.capture is not None:
            return self._err(422, "ORDER_ALREADY_CAPTURED")
        if o.status == "PAYER_ACTION_REQUIRED":
            return self._err(422, "PAYER_ACTION_REQUIRED")
        if o.status == "CREATED" and not (o.card or {}).get("number"):
            # Seen live (Oct 25): after a Card Fields submit with a 3-D Secure challenge the order stays
            # CREATED with the card attached, and the capture succeeds. Without a card it is not approved.
            return self._err(422, "ORDER_NOT_APPROVED")
        self._capture(o)
        return httpx.Response(201, json=self._order_json(o), headers={"paypal-debug-id": "dbg-cap"})

    def _authorize_route(self, request: httpx.Request, oid: str) -> httpx.Response:
        """Intent AUTHORIZE: a payment hold. Declines like a capture (CCREJECT-* name → DENIED)."""
        o = self.orders.get(oid)
        if o is None:
            return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND"})
        if o.intent != "AUTHORIZE":
            return self._err(422, "ACTION_DOES_NOT_MATCH_INTENT")
        if o.authorization is not None:
            return self._err(422, "ORDER_ALREADY_AUTHORIZED")
        if o.status == "PAYER_ACTION_REQUIRED":
            return self._err(422, "PAYER_ACTION_REQUIRED")
        if o.status == "CREATED" and not (o.card or {}).get("number"):
            return self._err(422, "ORDER_NOT_APPROVED")
        code = trigger_code((o.card or {}).get("name"))
        o.status = "COMPLETED"
        o.authorization = {
            "id": self._id("AUTH"),
            "status": "DENIED" if code else "CREATED",
            "amount": o.unit["amount"],
            "custom_id": o.unit.get("custom_id"),
            "processor_response": {"response_code": code or "0000"},
            "expiration_time": "2026-11-17T10:00:00Z",
        }
        self.authorizations[o.authorization["id"]] = o
        return httpx.Response(201, json=self._order_json(o))

    def _void_route(self, request: httpx.Request, aid: str) -> httpx.Response:
        o = self.authorizations.get(aid)
        if o is None or o.authorization is None:
            return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND"})
        if o.authorization["status"] != "CREATED":
            return self._err(422, f"AUTHORIZATION_ALREADY_{o.authorization['status']}")
        o.authorization["status"] = "VOIDED"
        return httpx.Response(204)

    def _capture_auth_route(self, request: httpx.Request, aid: str) -> httpx.Response:
        o = self.authorizations.get(aid)
        if o is None or o.authorization is None:
            return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND"})
        if o.authorization["status"] != "CREATED":
            return self._err(422, f"AUTHORIZATION_ALREADY_{o.authorization['status']}")
        o.authorization["status"] = "CAPTURED"
        cap = {
            "id": self._id("CAP"),
            "status": "COMPLETED",
            "amount": o.unit["amount"],
            "final_capture": True,
        }
        return httpx.Response(201, json=cap)

    def _dispute_answer(self, request: httpx.Request, did: str, verb: str) -> httpx.Response:
        """accept-claim takes JSON; provide-evidence takes multipart/form-data with an `input` JSON part."""
        if verb == "provide-evidence" and not request.headers.get("content-type", "").startswith(
            "multipart/form-data"
        ):
            return self._err(415, "UNSUPPORTED_MEDIA_TYPE")
        self.dispute_answers.append((did, verb, request.content))
        return httpx.Response(200, json={"links": [{"rel": "self", "href": f"/v1/customer/disputes/{did}"}]})

    def _get_order(self, request: httpx.Request, oid: str) -> httpx.Response:
        self.calls.append((request.method, request.url.path, None))
        o = self.orders.get(oid)
        if o is None:
            return httpx.Response(
                404, json={"name": "RESOURCE_NOT_FOUND", "details": [{"issue": "INVALID_RESOURCE_ID"}]}
            )
        return httpx.Response(200, json=self._order_json(o))

    def _refund(self, request: httpx.Request, cid: str) -> httpx.Response:
        cap = next((o.capture for o in self.orders.values() if o.capture and o.capture["id"] == cid), None)
        if cap is None or cap["status"] != "COMPLETED":
            return httpx.Response(
                404, json={"name": "RESOURCE_NOT_FOUND", "details": [{"issue": "INVALID_RESOURCE_ID"}]}
            )
        if cid in self.refunds:
            return self._err(422, "CAPTURE_FULLY_REFUNDED")
        ref_id = self._id("REF")
        self.refunds[cid] = {
            "id": ref_id,
            "status": "COMPLETED",
            "links": [
                {"href": f"{SANDBOX_API_BASE}/v2/payments/refunds/{ref_id}", "rel": "self", "method": "GET"},
                {"href": f"{SANDBOX_API_BASE}/v2/payments/captures/{cid}", "rel": "up", "method": "GET"},
            ],
        }
        return httpx.Response(201, json=self.refunds[cid])

    def _refund_route(self, request: httpx.Request, cid: str) -> httpx.Response:
        if self.refund_outage:
            self.refund_outage -= 1
            self.calls.append((request.method, request.url.path, request.headers.get("PayPal-Request-Id")))
            return httpx.Response(503, json={"name": "SERVICE_UNAVAILABLE", "details": []})
        res = self._idempotent(request, self._refund, cid=cid)
        if self.refund_answer_lost:
            self.refund_answer_lost -= 1
            return httpx.Response(503, json={"name": "SERVICE_UNAVAILABLE", "details": []})
        return res

    def _verify(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.verify_bodies.append(body)
        ok = body.get("transmission_sig") == self.valid_signature and body.get("webhook_id") == "WH-1"
        return httpx.Response(200, json={"verification_status": "SUCCESS" if ok else "FAILURE"})

    def install(self, router: respx.MockRouter) -> None:
        router.post("/v1/oauth2/token").mock(side_effect=self._token)
        router.post("/v2/checkout/orders").mock(side_effect=lambda r: self._idempotent(r, self._create))
        router.post(path__regex=r"^/v2/checkout/orders/(?P<oid>[^/]+)/capture$").mock(
            side_effect=lambda r, oid: self._idempotent(r, self._capture_route, oid=oid)
        )
        router.post(path__regex=r"^/v2/checkout/orders/(?P<oid>[^/]+)/authorize$").mock(
            side_effect=lambda r, oid: self._idempotent(r, self._authorize_route, oid=oid)
        )
        router.post(path__regex=r"^/v2/payments/authorizations/(?P<aid>[^/]+)/void$").mock(
            side_effect=lambda r, aid: self._idempotent(r, self._void_route, aid=aid)
        )
        router.post(path__regex=r"^/v2/payments/authorizations/(?P<aid>[^/]+)/capture$").mock(
            side_effect=lambda r, aid: self._idempotent(r, self._capture_auth_route, aid=aid)
        )
        router.post(
            path__regex=r"^/v1/customer/disputes/(?P<did>[^/]+)/(?P<verb>accept-claim|provide-evidence)$"
        ).mock(side_effect=lambda r, did, verb: self._idempotent(r, self._dispute_answer, did=did, verb=verb))
        router.get(path__regex=r"^/v2/checkout/orders/(?P<oid>[^/]+)$").mock(side_effect=self._get_order)
        router.post(path__regex=r"^/v2/payments/captures/(?P<cid>[^/]+)/refund$").mock(
            side_effect=lambda r, cid: self._refund_route(r, cid)
        )
        router.post("/v1/notifications/verify-webhook-signature").mock(side_effect=self._verify)
