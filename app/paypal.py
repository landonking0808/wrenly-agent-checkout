"""PayPal REST client (Orders v2, Payments v2, Payouts v1, Webhooks) with a faithful in-memory mock.

Every money-moving call carries a PayPal-Request-Id so a retry returns the original result instead of
moving money twice. Payouts also carry a deterministic sender_batch_id.
"""
from __future__ import annotations

import base64
import threading
import time
import uuid

import httpx

from . import config


class PayPalError(RuntimeError):
    def __init__(self, message: str, status: int = 0, body: dict | None = None, debug_id: str | None = None):
        super().__init__(message)
        self.status, self.body, self.debug_id = status, body or {}, debug_id


def _money(v: float) -> dict:
    return {"currency_code": "USD", "value": f"{v:.2f}"}


class RealPayPal:
    mode = "sandbox"

    def __init__(self, client_id: str, secret: str, base: str):
        self.client_id, self.secret, self.base = client_id, secret, base
        self.mode = "live" if "sandbox" not in base else "sandbox"
        self._token, self._token_exp = None, 0.0
        self._http = httpx.Client(timeout=45)

    def token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        auth = base64.b64encode(f"{self.client_id}:{self.secret}".encode()).decode()
        r = self._http.post(f"{self.base}/v1/oauth2/token", data={"grant_type": "client_credentials"},
                            headers={"Authorization": f"Basic {auth}"})
        if r.status_code != 200:
            raise PayPalError(f"PayPal auth failed ({r.status_code})", r.status_code, _safe_json(r))
        d = r.json()
        self._token, self._token_exp = d["access_token"], time.time() + int(d.get("expires_in", 3000))
        return self._token

    def _call(self, method: str, path: str, body: dict | None = None, request_id: str | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json", "Prefer": "return=representation"}
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        r = self._http.request(method, f"{self.base}{path}", json=body, headers=headers)
        if r.status_code >= 400:
            d = _safe_json(r)
            msg = d.get("message") or d.get("name") or r.text[:200]
            details = "; ".join(f"{x.get('issue')}: {x.get('description')}" for x in d.get("details", []) if isinstance(x, dict))
            raise PayPalError(f"PayPal {method} {path} -> {r.status_code}: {msg} {details}".strip(), r.status_code, d, r.headers.get("PayPal-Debug-Id"))
        return _safe_json(r)

    # Orders v2
    def create_order(self, amount: float, description: str, reference_id: str, request_id: str, payment_source: dict | None = None) -> dict:
        body = {
            "intent": "AUTHORIZE",
            "purchase_units": [{"reference_id": reference_id, "custom_id": reference_id, "description": description[:127],
                                "amount": _money(amount), "soft_descriptor": "WRENLY HOME"}],
        }
        if payment_source:
            body["payment_source"] = payment_source
        else:
            body["payment_source"] = {"paypal": {"experience_context": {
                "brand_name": "Wrenly Home", "shipping_preference": "NO_SHIPPING", "user_action": "CONTINUE",
                "return_url": config.env("PUBLIC_URL", "http://localhost:8000") + "/?approved=1",
                "cancel_url": config.env("PUBLIC_URL", "http://localhost:8000") + "/?cancel=1"}}}
        return self._call("POST", "/v2/checkout/orders", body, request_id)

    def get_order(self, order_id: str) -> dict:
        return self._call("GET", f"/v2/checkout/orders/{order_id}")

    def authorize_order(self, order_id: str, request_id: str) -> dict:
        return self._call("POST", f"/v2/checkout/orders/{order_id}/authorize", {}, request_id)

    # Payments v2
    def capture_authorization(self, auth_id: str, amount: float, invoice_id: str, request_id: str) -> dict:
        return self._call("POST", f"/v2/payments/authorizations/{auth_id}/capture",
                          {"amount": _money(amount), "invoice_id": invoice_id, "final_capture": True}, request_id)

    def void_authorization(self, auth_id: str, request_id: str) -> dict:
        return self._call("POST", f"/v2/payments/authorizations/{auth_id}/void", None, request_id)

    def refund_capture(self, capture_id: str, amount: float, note: str, request_id: str) -> dict:
        return self._call("POST", f"/v2/payments/captures/{capture_id}/refund",
                          {"amount": _money(amount), "note_to_payer": note[:255]}, request_id)

    # Payouts v1
    def create_payout(self, sender_batch_id: str, receiver_email: str, amount: float, note: str, item_id: str) -> dict:
        body = {
            "sender_batch_header": {"sender_batch_id": sender_batch_id, "email_subject": "Wrenly Home: you've been paid",
                                    "email_message": note[:255]},
            "items": [{"recipient_type": "EMAIL", "receiver": receiver_email, "amount": _money(amount),
                       "note": note[:255], "sender_item_id": item_id}],
        }
        return self._call("POST", "/v1/payments/payouts", body, sender_batch_id)

    def get_payout(self, batch_id: str) -> dict:
        return self._call("GET", f"/v1/payments/payouts/{batch_id}")

    # Webhooks
    def verify_webhook(self, headers: dict, event: dict, webhook_id: str) -> bool:
        h = {k.lower(): v for k, v in headers.items()}
        body = {"auth_algo": h.get("paypal-auth-algo"), "cert_url": h.get("paypal-cert-url"),
                "transmission_id": h.get("paypal-transmission-id"), "transmission_sig": h.get("paypal-transmission-sig"),
                "transmission_time": h.get("paypal-transmission-time"), "webhook_id": webhook_id, "webhook_event": event}
        return self._call("POST", "/v1/notifications/verify-webhook-signature", body).get("verification_status") == "SUCCESS"


def _safe_json(r: httpx.Response) -> dict:
    try:
        return r.json()
    except Exception:
        return {"raw": r.text[:500]}


class MockPayPal:
    """In-memory PayPal with the same shapes, state machine and PayPal-Request-Id replay as the real API."""
    mode = "mock"

    def __init__(self):
        self.orders: dict[str, dict] = {}
        self.auths: dict[str, dict] = {}
        self.captures: dict[str, dict] = {}
        self.refunds: dict[str, dict] = {}
        self.payouts: dict[str, dict] = {}
        self._replay: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []  # (operation, request_id) for tests: what actually hit "PayPal"
        self.money_moves: list[tuple[str, float]] = []
        self.fail_next: str | None = None
        self._lock = threading.RLock()

    @staticmethod
    def _id(prefix: str) -> str:
        return (prefix + uuid.uuid4().hex.upper())[:17]

    def _replayed(self, op: str, request_id: str | None):
        self.calls.append((op, request_id or ""))
        if self.fail_next == op:
            self.fail_next = None
            raise PayPalError(f"mock: simulated network failure on {op}", 503)
        if request_id and (op, request_id) in self._replay:
            return self._replay[(op, request_id)]
        return None

    def _remember(self, op, request_id, resp):
        if request_id:
            self._replay[(op, request_id)] = resp
        return resp

    def create_order(self, amount, description, reference_id, request_id, payment_source=None):
        with self._lock:
            if (r := self._replayed("create_order", request_id)) is not None:
                return r
            oid = self._id("MOCKORD")
            o = {"id": oid, "intent": "AUTHORIZE", "status": "PAYER_ACTION_REQUIRED",
                 "purchase_units": [{"reference_id": reference_id, "description": description, "amount": _money(amount)}],
                 "links": [{"rel": "payer-action", "href": f"/mock-paypal/approve/{oid}"}]}
            self.orders[oid] = o
            if payment_source and "card" in payment_source:  # PayPal authorizes card orders inside the create call
                aid = self._id("MOCKAUTH")
                self.auths[aid] = {"id": aid, "status": "CREATED", "amount": _money(amount), "order_id": oid}
                o["status"] = "COMPLETED"
                o["purchase_units"][0]["payments"] = {"authorizations": [dict(self.auths[aid])]}
            return self._remember("create_order", request_id, dict(o))

    def approve(self, order_id: str) -> None:
        """What the buyer does in the PayPal popup."""
        with self._lock:
            o = self.orders[order_id]
            o["status"] = "APPROVED"
            o["payer"] = {"email_address": "sb-buyer@personal.example.com", "payer_id": "MOCKPAYER01"}

    def get_order(self, order_id):
        if order_id not in self.orders:
            raise PayPalError("RESOURCE_NOT_FOUND", 404)
        return dict(self.orders[order_id])

    def authorize_order(self, order_id, request_id):
        with self._lock:
            if (r := self._replayed("authorize_order", request_id)) is not None:
                return r
            o = self.orders.get(order_id)
            if not o:
                raise PayPalError("RESOURCE_NOT_FOUND", 404)
            if o["status"] != "APPROVED":
                raise PayPalError("UNPROCESSABLE_ENTITY: ORDER_NOT_APPROVED", 422)
            aid = self._id("MOCKAUTH")
            amt = o["purchase_units"][0]["amount"]
            self.auths[aid] = {"id": aid, "status": "CREATED", "amount": amt, "order_id": order_id}
            o["status"] = "COMPLETED"
            o["purchase_units"][0]["payments"] = {"authorizations": [dict(self.auths[aid])]}
            return self._remember("authorize_order", request_id, dict(o))

    def capture_authorization(self, auth_id, amount, invoice_id, request_id):
        with self._lock:
            if (r := self._replayed("capture", request_id)) is not None:
                return r
            a = self.auths.get(auth_id)
            if not a:
                raise PayPalError("RESOURCE_NOT_FOUND", 404)
            if a["status"] != "CREATED":
                raise PayPalError(f"UNPROCESSABLE_ENTITY: AUTHORIZATION_ALREADY_{a['status']}", 422)
            if amount > float(a["amount"]["value"]) + 0.001:
                raise PayPalError("UNPROCESSABLE_ENTITY: AMOUNT_EXCEEDS_AUTHORIZED", 422)
            cid = self._id("MOCKCAP")
            a["status"] = "CAPTURED"
            c = {"id": cid, "status": "COMPLETED", "amount": _money(amount), "invoice_id": invoice_id, "final_capture": True,
                 "seller_receivable_breakdown": {"paypal_fee": _money(round(amount * 0.0349 + 0.49, 2))}}
            self.captures[cid] = c
            self.money_moves.append(("capture", amount))
            return self._remember("capture", request_id, dict(c))

    def void_authorization(self, auth_id, request_id):
        with self._lock:
            if (r := self._replayed("void", request_id)) is not None:
                return r
            a = self.auths[auth_id]
            a["status"] = "VOIDED"
            return self._remember("void", request_id, {})

    def refund_capture(self, capture_id, amount, note, request_id):
        with self._lock:
            if (r := self._replayed("refund", request_id)) is not None:
                return r
            c = self.captures.get(capture_id)
            if not c:
                raise PayPalError("RESOURCE_NOT_FOUND", 404)
            done = sum(float(x["amount"]["value"]) for x in self.refunds.values() if x["capture_id"] == capture_id)
            if done + amount > float(c["amount"]["value"]) + 0.001:
                raise PayPalError("UNPROCESSABLE_ENTITY: REFUND_AMOUNT_EXCEEDED", 422)
            rid = self._id("MOCKREF")
            self.refunds[rid] = {"id": rid, "status": "COMPLETED", "amount": _money(amount), "capture_id": capture_id}
            c["status"] = "PARTIALLY_REFUNDED" if done + amount < float(c["amount"]["value"]) else "REFUNDED"
            self.money_moves.append(("refund", amount))
            return self._remember("refund", request_id, {k: v for k, v in self.refunds[rid].items() if k != "capture_id"})

    def create_payout(self, sender_batch_id, receiver_email, amount, note, item_id):
        with self._lock:
            if (r := self._replayed("payout", sender_batch_id)) is not None:
                return r
            if any(p["batch_header"]["sender_batch_header"]["sender_batch_id"] == sender_batch_id for p in self.payouts.values()):
                raise PayPalError("USER_BUSINESS_ERROR: Batch with given sender_batch_id already exists", 400)
            bid = self._id("MOCKBATCH")
            p = {"batch_header": {"payout_batch_id": bid, "batch_status": "PENDING",
                                  "sender_batch_header": {"sender_batch_id": sender_batch_id}},
                 "items": [{"payout_item_id": self._id("MOCKITEM"), "transaction_status": "SUCCESS",
                            "payout_item": {"receiver": receiver_email, "amount": _money(amount), "sender_item_id": item_id}}]}
            self.payouts[bid] = p
            self.money_moves.append(("payout", amount))
            return self._remember("payout", sender_batch_id, {"batch_header": p["batch_header"]})

    def get_payout(self, batch_id):
        p = self.payouts[batch_id]
        p["batch_header"]["batch_status"] = "SUCCESS"
        return p

    def verify_webhook(self, headers, event, webhook_id):
        return headers.get("paypal-transmission-sig", headers.get("PAYPAL-TRANSMISSION-SIG")) == "mock-valid"


_client = None
_client_lock = threading.Lock()


def client():
    global _client
    with _client_lock:
        if _client is None:
            if config.mock_paypal():
                _client = MockPayPal()
            else:
                _client = RealPayPal(config.env("PAYPAL_CLIENT_ID"), config.env("PAYPAL_CLIENT_SECRET"), config.paypal_base())
        return _client


def set_client(c) -> None:
    global _client
    _client = c
