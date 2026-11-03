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


MOCK_BASE = "/mock-paypal"  # local, clearly-not-PayPal host for the mock's HATEOAS links


def _link(path: str, rel: str, method: str = "GET") -> dict:
    return {"href": f"{MOCK_BASE}{path}", "rel": rel, "method": method}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _later(days: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + days * 86400))


def _mock_error(status: int, name: str, issue: str, description: str, message: str | None = None) -> PayPalError:
    """An error with PayPal's documented error body: name, message, debug_id, details[{issue, description}]."""
    debug_id = "mock" + uuid.uuid4().hex[:9]
    body = {"name": name, "message": message or description, "debug_id": debug_id,
            "details": [{"issue": issue, "description": description}]}
    return PayPalError(f"{name}: {issue}" if not message else message, status, body, debug_id)


class MockPayPal:
    """In-memory PayPal built to the documented REST shapes, state machine and PayPal-Request-Id replay.

    These are simulated responses: no request leaves the process and no money moves. Ids carry a MOCK prefix.
    """
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
            raise PayPalError(f"mock: simulated network failure on {op}", 503, {"name": "SERVICE_UNAVAILABLE"}, "mock-503")
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
            # The client always sends payment_source.paypal.experience_context, for which PayPal answers
            # PAYER_ACTION_REQUIRED with a payer-action link (a bare order would be CREATED with an approve link).
            o = {"id": oid, "intent": "AUTHORIZE", "status": "PAYER_ACTION_REQUIRED",
                 "create_time": _now(),
                 "purchase_units": [{"reference_id": reference_id, "custom_id": reference_id, "description": description,
                                     "amount": _money(amount), "soft_descriptor": "WRENLY HOME"}],
                 "links": [_link(f"/v2/checkout/orders/{oid}", "self"),
                           _link(f"/checkoutnow?token={oid}", "payer-action"),
                           _link(f"/v2/checkout/orders/{oid}", "update", "PATCH"),
                           _link(f"/v2/checkout/orders/{oid}/authorize", "authorize", "POST")]}
            self.orders[oid] = o
            if payment_source and "card" in payment_source:  # PayPal authorizes card orders inside the create call
                aid = self._id("MOCKAUTH")
                self.auths[aid] = self._new_auth(aid, _money(amount), oid)
                o["status"] = "COMPLETED"
                o["links"] = [_link(f"/v2/checkout/orders/{oid}", "self")]
                o["purchase_units"][0]["payments"] = {"authorizations": [self._auth_view(aid)]}
            return self._remember("create_order", request_id, dict(o))

    def _new_auth(self, aid: str, amount: dict, order_id: str) -> dict:
        return {"id": aid, "status": "CREATED", "amount": amount, "order_id": order_id,
                "create_time": _now(), "update_time": _now(), "expiration_time": _later(29),
                "seller_protection": {"status": "ELIGIBLE", "dispute_categories": ["ITEM_NOT_RECEIVED", "UNAUTHORIZED_TRANSACTION"]}}

    def _auth_view(self, aid: str) -> dict:
        a = dict(self.auths[aid])
        a.pop("order_id", None)
        a["links"] = [_link(f"/v2/payments/authorizations/{aid}", "self"),
                      _link(f"/v2/payments/authorizations/{aid}/capture", "capture", "POST"),
                      _link(f"/v2/payments/authorizations/{aid}/void", "void", "POST"),
                      _link(f"/v2/payments/authorizations/{aid}/reauthorize", "reauthorize", "POST"),
                      _link(f"/v2/checkout/orders/{self.auths[aid]['order_id']}", "up")]
        return a

    def approve(self, order_id: str) -> None:
        """What the buyer does in the PayPal popup."""
        with self._lock:
            o = self.orders[order_id]
            o["status"] = "APPROVED"
            o["payer"] = {"name": {"given_name": "Mock", "surname": "Buyer"},
                          "email_address": "demo-buyer@personal.example.com", "payer_id": "MOCKPAYER01"}
            o["links"] = [_link(f"/v2/checkout/orders/{order_id}", "self"),
                          _link(f"/v2/checkout/orders/{order_id}", "update", "PATCH"),
                          _link(f"/v2/checkout/orders/{order_id}/authorize", "authorize", "POST")]

    def get_order(self, order_id):
        if order_id not in self.orders:
            raise _mock_error(404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID", "Specified resource ID does not exist.",
                              "RESOURCE_NOT_FOUND")
        return dict(self.orders[order_id])

    def authorize_order(self, order_id, request_id):
        with self._lock:
            if (r := self._replayed("authorize_order", request_id)) is not None:
                return r
            o = self.orders.get(order_id)
            if not o:
                raise _mock_error(404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID", "Specified resource ID does not exist.",
                                  "RESOURCE_NOT_FOUND")
            if o["status"] == "COMPLETED":
                raise _mock_error(422, "UNPROCESSABLE_ENTITY", "ORDER_ALREADY_AUTHORIZED", "Order already authorized.")
            if o["status"] != "APPROVED":
                raise _mock_error(422, "UNPROCESSABLE_ENTITY", "ORDER_NOT_APPROVED",
                                  "Payer has not yet approved the Order for payment.")
            aid = self._id("MOCKAUTH")
            amt = o["purchase_units"][0]["amount"]
            self.auths[aid] = self._new_auth(aid, amt, order_id)
            o["status"] = "COMPLETED"
            o["update_time"] = _now()
            o["links"] = [_link(f"/v2/checkout/orders/{order_id}", "self")]
            o["purchase_units"][0]["payments"] = {"authorizations": [self._auth_view(aid)]}
            return self._remember("authorize_order", request_id, dict(o))

    def capture_authorization(self, auth_id, amount, invoice_id, request_id):
        with self._lock:
            if (r := self._replayed("capture", request_id)) is not None:
                return r
            a = self.auths.get(auth_id)
            if not a:
                raise _mock_error(404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID", "Specified resource ID does not exist.",
                                  "RESOURCE_NOT_FOUND")
            if a["status"] != "CREATED":
                raise _mock_error(422, "UNPROCESSABLE_ENTITY", f"AUTHORIZATION_ALREADY_{a['status']}",
                                  f"Authorization has already been {a['status'].lower()}.")
            if amount > float(a["amount"]["value"]) + 0.001:
                raise _mock_error(422, "UNPROCESSABLE_ENTITY", "AMOUNT_EXCEEDS_AUTHORIZED",
                                  "Capture amount exceeds the authorized amount.")
            cid = self._id("MOCKCAP")
            a["status"] = "CAPTURED"
            a["update_time"] = _now()
            fee = round(amount * 0.0349 + 0.49, 2)
            c = {"id": cid, "status": "COMPLETED", "amount": _money(amount), "invoice_id": invoice_id, "final_capture": True,
                 "seller_protection": a.get("seller_protection", {"status": "ELIGIBLE"}),
                 "seller_receivable_breakdown": {"gross_amount": _money(amount), "paypal_fee": _money(fee),
                                                 "net_amount": _money(round(amount - fee, 2))},
                 "create_time": _now(), "update_time": _now(),
                 "links": [_link(f"/v2/payments/captures/{cid}", "self"),
                           _link(f"/v2/payments/captures/{cid}/refund", "refund", "POST"),
                           _link(f"/v2/payments/authorizations/{auth_id}", "up")]}
            self.captures[cid] = c
            self.money_moves.append(("capture", amount))
            return self._remember("capture", request_id, dict(c))

    def void_authorization(self, auth_id, request_id):
        with self._lock:
            if (r := self._replayed("void", request_id)) is not None:
                return r
            a = self.auths.get(auth_id)
            if not a:
                raise _mock_error(404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID", "Specified resource ID does not exist.",
                                  "RESOURCE_NOT_FOUND")
            if a["status"] != "CREATED":
                raise _mock_error(422, "UNPROCESSABLE_ENTITY", f"AUTHORIZATION_ALREADY_{a['status']}",
                                  f"Authorization has already been {a['status'].lower()}.")
            a["status"] = "VOIDED"
            a["update_time"] = _now()
            # The client sends Prefer: return=representation, so PayPal returns 200 with the voided authorization.
            return self._remember("void", request_id, {k: v for k, v in self._auth_view(auth_id).items() if k != "links"}
                                  | {"links": [_link(f"/v2/payments/authorizations/{auth_id}", "self")]})

    def refund_capture(self, capture_id, amount, note, request_id):
        with self._lock:
            if (r := self._replayed("refund", request_id)) is not None:
                return r
            c = self.captures.get(capture_id)
            if not c:
                raise _mock_error(404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID", "Specified resource ID does not exist.",
                                  "RESOURCE_NOT_FOUND")
            done = sum(float(x["amount"]["value"]) for x in self.refunds.values() if x["capture_id"] == capture_id)
            if done + amount > float(c["amount"]["value"]) + 0.001:
                raise _mock_error(422, "UNPROCESSABLE_ENTITY", "REFUND_AMOUNT_EXCEEDED",
                                  "The refund amount must be less than or equal to the capture amount that has not yet been refunded.")
            rid = self._id("MOCKREF")
            self.refunds[rid] = {"id": rid, "status": "COMPLETED", "amount": _money(amount), "capture_id": capture_id,
                                 "note_to_payer": note[:255], "create_time": _now(), "update_time": _now(),
                                 "links": [_link(f"/v2/payments/refunds/{rid}", "self"),
                                           _link(f"/v2/payments/captures/{capture_id}", "up")]}
            c["status"] = "PARTIALLY_REFUNDED" if done + amount < float(c["amount"]["value"]) else "REFUNDED"
            self.money_moves.append(("refund", amount))
            return self._remember("refund", request_id, {k: v for k, v in self.refunds[rid].items() if k != "capture_id"})

    def create_payout(self, sender_batch_id, receiver_email, amount, note, item_id):
        with self._lock:
            if (r := self._replayed("payout", sender_batch_id)) is not None:
                return r
            if any(p["batch_header"]["sender_batch_header"]["sender_batch_id"] == sender_batch_id for p in self.payouts.values()):
                raise _mock_error(400, "USER_BUSINESS_ERROR", "DUPLICATE_SENDER_BATCH_ID",
                                  "Batch with given sender_batch_id already exists",
                                  "USER_BUSINESS_ERROR: Batch with given sender_batch_id already exists")
            bid = self._id("MOCKBATCH")
            hdr = {"payout_batch_id": bid, "batch_status": "PENDING", "time_created": _now(),
                   "sender_batch_header": {"sender_batch_id": sender_batch_id, "email_subject": "Wrenly Home: you've been paid",
                                           "email_message": note[:255]}}
            iid = self._id("MOCKITEM")
            p = {"batch_header": hdr,
                 "items": [{"payout_item_id": iid, "transaction_id": self._id("MOCKTXN"), "transaction_status": "SUCCESS",
                            "payout_batch_id": bid, "payout_item_fee": _money(0.25),
                            "payout_item": {"recipient_type": "EMAIL", "receiver": receiver_email, "amount": _money(amount),
                                            "note": note[:255], "sender_item_id": item_id},
                            "links": [_link(f"/v1/payments/payouts-item/{iid}", "item")]}],
                 "links": [_link(f"/v1/payments/payouts/{bid}", "self")]}
            self.payouts[bid] = p
            self.money_moves.append(("payout", amount))
            # POST /v1/payments/payouts returns 201 with only the batch_header (status PENDING) and a self link.
            return self._remember("payout", sender_batch_id,
                                  {"batch_header": dict(hdr), "links": [_link(f"/v1/payments/payouts/{bid}", "self", "GET")]})

    def get_payout(self, batch_id):
        if batch_id not in self.payouts:
            raise _mock_error(404, "INVALID_RESOURCE_ID", "INVALID_RESOURCE_ID", "Requested resource ID was not found.",
                              "INVALID_RESOURCE_ID")
        p = self.payouts[batch_id]
        p["batch_header"]["batch_status"] = "SUCCESS"
        p["batch_header"]["time_completed"] = _now()
        amt = sum(float(i["payout_item"]["amount"]["value"]) for i in p["items"])
        p["batch_header"]["amount"] = _money(amt)
        p["batch_header"]["fees"] = _money(0.25 * len(p["items"]))
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
