"""The guarded money layer. This is the ONLY module that moves money, and every AI tool goes through it.

Guardrails enforced here, in code, no matter what the AI asks for:
  * order amount must equal the server-side quote and stay under max_order_amount
  * capture only after the crew marks the job done with a complete checklist
  * crew payout only after capture AND a passing margin check; otherwise it is held for a human
  * refunds are only ever *proposed* by the agent; a human approves them in the dashboard
  * every call uses a deterministic PayPal-Request-Id / sender_batch_id, and the ledger is checked first,
    so a retry (by a person, the agent, or a crashed worker) never charges or pays twice
"""
from __future__ import annotations

import json

from . import config, db, margin as margin_rule
from .paypal import PayPalError, client


class GuardrailError(RuntimeError):
    """The agent (or a person) asked for something the money rules do not allow."""


def _pp():
    return client()


def _blocked(job_id, action, why, actor, **detail):
    db.ledger_append(action, "blocked", job_id=job_id, actor=actor, detail={"reason": why, **detail})
    raise GuardrailError(why)


def _once(action: str, request_id: str, job_id: str, actor: str):
    """Return the earlier successful ledger row for this request id (and record the deduped retry), or None."""
    prev = db.ledger_find_ok(request_id)
    if prev:
        db.ledger_append(action, "deduped", job_id=job_id, amount=prev["amount"], paypal_id=prev["paypal_id"],
                         request_id=request_id, actor=actor,
                         detail={"reason": "already done under this PayPal-Request-Id; nothing sent to PayPal", "original_seq": prev["seq"]})
        prev = dict(prev)
        prev["detail"] = json.loads(prev["detail"] or "{}")
        prev["deduped"] = True
    return prev


def _job(job_id: str) -> dict:
    j = db.job(job_id)
    if not j:
        raise GuardrailError(f"no job {job_id}")
    return j


# ---- checkout -----------------------------------------------------------

def create_order(job_id: str, actor: str = "agent", payment_source: dict | None = None) -> dict:
    """Create a PayPal Orders v2 order with intent AUTHORIZE for exactly the server-side quote.

    payment_source is only used by the API-only sandbox test (a sandbox test card), where PayPal
    authorizes in the same call; the web checkout always leaves it empty so the buyer approves in PayPal."""
    j = _job(job_id)
    pol = config.policy()
    amount = float(j["list_price"])
    if amount <= 0 or amount > pol["max_order_amount"]:
        _blocked(job_id, "order.create", f"order amount ${amount:.2f} outside 0-${pol['max_order_amount']:.2f}", actor)
    rid = f"wrenly-{job_id}-order"
    if prev := _once("order.create", rid, job_id, actor):
        return {"order_id": prev["paypal_id"], "deduped": True}
    desc = f"Wrenly Home cleaning: {j['bedrooms']} bed, {j['frequency'].replace('_', '-')} ({job_id})"
    try:
        o = _pp().create_order(amount, desc, job_id, rid, payment_source)
    except PayPalError as e:
        db.ledger_append("order.create", "error", job_id=job_id, amount=amount, request_id=rid, actor=actor, detail={"error": str(e), "debug_id": e.debug_id})
        raise
    db.job_update(job_id, order_id=o["id"], status="awaiting_payment")
    db.ledger_append("order.create", "ok", job_id=job_id, amount=amount, paypal_id=o["id"], request_id=rid, actor=actor,
                     detail={"intent": "AUTHORIZE", "paypal_status": o.get("status"), "payment_source": "card" if payment_source else "paypal"})
    auths = ((o.get("purchase_units") or [{}])[0].get("payments") or {}).get("authorizations") or []
    if auths:  # card payment source: PayPal authorized inside the create call
        _record_authorization(job_id, o["id"], auths[0], f"wrenly-{job_id}-authorize", actor)
    return {"order_id": o["id"], "status": o.get("status"), "approve_url": next((l["href"] for l in o.get("links", []) if l.get("rel") in ("payer-action", "approve")), None)}


def _record_authorization(job_id: str, order_id: str, auth: dict, rid: str, actor: str) -> dict:
    j = _job(job_id)
    amt = float(auth["amount"]["value"])
    if abs(amt - float(j["list_price"])) > 0.005:
        db.ledger_append("order.authorize", "error", job_id=job_id, amount=amt, paypal_id=auth["id"], request_id=rid, actor=actor,
                         detail={"error": "authorized amount differs from quote"})
        raise GuardrailError("authorized amount differs from quote")
    if auth.get("status") not in (None, "CREATED", "PENDING"):
        db.ledger_append("order.authorize", "error", job_id=job_id, amount=amt, paypal_id=auth["id"], request_id=rid, actor=actor,
                         detail={"error": f"authorization status {auth.get('status')}"})
        raise GuardrailError(f"authorization status {auth.get('status')}")
    db.job_update(job_id, authorization_id=auth["id"], status="authorized")
    db.ledger_append("order.authorize", "ok", job_id=job_id, amount=amt, paypal_id=auth["id"], request_id=rid, actor=actor,
                     detail={"order_id": order_id, "auth_status": auth.get("status"), "expiration_time": auth.get("expiration_time")})
    return {"authorization_id": auth["id"], "status": auth.get("status")}


def authorize(job_id: str, order_id: str, actor: str = "customer") -> dict:
    j = _job(job_id)
    if j["order_id"] != order_id:
        _blocked(job_id, "order.authorize", "order id does not belong to this job", actor, order_id=order_id)
    rid = f"wrenly-{job_id}-authorize"
    if prev := _once("order.authorize", rid, job_id, actor):
        return {"authorization_id": prev["paypal_id"], "deduped": True}
    try:
        o = _pp().authorize_order(order_id, rid)
    except PayPalError as e:
        db.ledger_append("order.authorize", "error", job_id=job_id, request_id=rid, actor=actor, detail={"error": str(e), "debug_id": e.debug_id})
        raise
    return _record_authorization(job_id, order_id, o["purchase_units"][0]["payments"]["authorizations"][0], rid, actor)


def void(job_id: str, actor: str = "ops") -> dict:
    j = _job(job_id)
    if not j["authorization_id"] or j["capture_id"]:
        _blocked(job_id, "authorization.void", "only an uncaptured authorization can be voided", actor)
    rid = f"wrenly-{job_id}-void"
    if _once("authorization.void", rid, job_id, actor):
        return {"voided": True, "deduped": True}
    _pp().void_authorization(j["authorization_id"], rid)
    db.job_update(job_id, status="cancelled")
    db.ledger_append("authorization.void", "ok", job_id=job_id, amount=j["list_price"], paypal_id=j["authorization_id"], request_id=rid, actor=actor)
    return {"voided": True}


# ---- completion ---------------------------------------------------------

def mark_done(job_id: str, checklist: dict, extra_hours: float = 0.0, actor: str = "crew") -> dict:
    j = _job(job_id)
    if j["status"] not in ("authorized",):
        _blocked(job_id, "job.complete", f"job is '{j['status']}', it must be authorized (paid) before it can be completed", actor)
    required = config.policy()["checklist"]
    missing = [c for c in required if not checklist.get(c)]
    if extra_hours < 0 or extra_hours > 8:
        _blocked(job_id, "job.complete", "extra hours must be between 0 and 8", actor)
    db.job_update(job_id, checklist_json=json.dumps(checklist), extra_hours=float(extra_hours),
                  status="completed" if not missing else "authorized")
    db.ledger_append("job.complete", "ok" if not missing else "blocked", job_id=job_id, actor=actor,
                     detail={"checklist_done": len(required) - len(missing), "checklist_total": len(required),
                             "missing": missing, "extra_hours": extra_hours})
    if missing:
        raise GuardrailError(f"checklist incomplete: {', '.join(missing)}")
    return {"status": "completed"}


def capture(job_id: str, actor: str = "agent") -> dict:
    j = _job(job_id)
    rid = f"wrenly-{job_id}-capture"
    if prev := _once("payment.capture", rid, job_id, actor):
        return {"capture_id": prev["paypal_id"], "amount": prev["amount"], "deduped": True}
    if j["status"] != "completed":
        _blocked(job_id, "payment.capture", f"cannot capture: job is '{j['status']}', the crew has not completed it", actor)
    if not j["authorization_id"]:
        _blocked(job_id, "payment.capture", "no authorization on this job", actor)
    amount = float(j["list_price"])
    try:
        c = _pp().capture_authorization(j["authorization_id"], amount, f"wrenly-{job_id}", rid)
    except PayPalError as e:
        db.ledger_append("payment.capture", "error", job_id=job_id, amount=amount, request_id=rid, actor=actor, detail={"error": str(e), "debug_id": e.debug_id})
        raise
    fee = (c.get("seller_receivable_breakdown") or {}).get("paypal_fee", {}).get("value")
    db.job_update(job_id, capture_id=c["id"], captured_amount=amount, status="captured")
    db.ledger_append("payment.capture", "ok", job_id=job_id, amount=amount, paypal_id=c["id"], request_id=rid, actor=actor,
                     detail={"authorization_id": j["authorization_id"], "paypal_status": c.get("status"), "paypal_fee": fee})
    return {"capture_id": c["id"], "amount": amount, "status": c.get("status")}


def check_margin(job_id: str, actor: str = "agent") -> dict:
    j = _job(job_id)
    m = margin_rule.check(j["list_price"], j["captured_amount"] or 0, j["refunded_amount"] or 0, j["extra_hours"] or 0)
    db.job_update(job_id, margin_json=json.dumps(m))
    db.ledger_append("margin.check", "pass" if m["ok"] else "fail", job_id=job_id, amount=m["margin"], actor=actor,
                     detail={k: m[k] for k in ("crew_pay", "paypal_fee_est", "payout_fee_est", "margin", "floor", "reasons")})
    return m


def send_payout(job_id: str, actor: str = "agent", approved_by: str | None = None) -> dict:
    """Pay the crew. Without a human approval this only runs when capture is done and the margin check passes."""
    j = _job(job_id)
    batch_id = f"wrenly-{job_id}-payout"
    if prev := _once("payout.send", batch_id, job_id, actor):
        return {"payout_batch_id": prev["paypal_id"], "amount": prev["amount"], "deduped": True}
    if not j["capture_id"]:
        _blocked(job_id, "payout.send", "cannot pay the crew before the customer's payment is captured", actor)
    if not j["crew_email"]:
        _blocked(job_id, "payout.send", "no crew PayPal email on file", actor)
    m = margin_rule.check(j["list_price"], j["captured_amount"], j["refunded_amount"] or 0, j["extra_hours"] or 0)
    if not m["ok"] and not approved_by:
        _blocked(job_id, "payout.send", "margin check failed: " + "; ".join(m["reasons"]), actor, margin=m["margin"], floor=m["floor"])
    amount = m["crew_pay"]
    cap = config.policy()["max_payout_amount"] * (1.5 if approved_by else 1)
    if amount > cap:
        _blocked(job_id, "payout.send", f"payout ${amount:.2f} over hard cap ${cap:.2f}", actor)
    note = f"Wrenly Home crew pay for job {job_id} ({j['service_date'] or 'clean'}). Thank you!"
    try:
        p = _pp().create_payout(batch_id, j["crew_email"], amount, note, f"{job_id}-crew")
    except PayPalError as e:
        db.ledger_append("payout.send", "error", job_id=job_id, amount=amount, request_id=batch_id, actor=actor, detail={"error": str(e), "debug_id": e.debug_id})
        raise
    hdr = p["batch_header"]
    db.job_update(job_id, payout_batch_id=hdr["payout_batch_id"], payout_amount=amount, status="paid_out")
    db.ledger_append("payout.send", "ok", job_id=job_id, amount=amount, paypal_id=hdr["payout_batch_id"], request_id=batch_id, actor=actor,
                     detail={"sender_batch_id": batch_id, "batch_status": hdr.get("batch_status"), "receiver": j["crew_email"],
                             "approved_by": approved_by, "margin": m["margin"]})
    return {"payout_batch_id": hdr["payout_batch_id"], "amount": amount, "batch_status": hdr.get("batch_status")}


def hold_payout(job_id: str, reason: str, actor: str = "agent") -> dict:
    j = _job(job_id)
    existing = db.q1("SELECT id FROM approvals WHERE job_id=? AND kind='payout' AND status='pending'", (job_id,))
    if existing:
        return {"approval_id": existing["id"], "deduped": True}
    m = j["margin"] or margin_rule.check(j["list_price"], j["captured_amount"] or 0, j["refunded_amount"] or 0, j["extra_hours"] or 0)
    aid = db.new_id("apr")
    db.ex("INSERT INTO approvals (id, created_at, job_id, kind, amount, reason, proposal_json, status) VALUES (?,?,?,?,?,?,?,?)",
          (aid, db.now_iso(), job_id, "payout", m["crew_pay"], reason, json.dumps({"margin": m}), "pending"))
    db.job_update(job_id, status="payout_held")
    db.ledger_append("payout.hold", "pending_approval", job_id=job_id, amount=m["crew_pay"], actor=actor, detail={"approval_id": aid, "reason": reason})
    return {"approval_id": aid, "status": "pending"}


# ---- refunds ------------------------------------------------------------

def refundable(j: dict) -> float:
    pending = sum(a["amount"] for a in db.approvals("pending") if a["job_id"] == j["id"] and a["kind"] == "refund")
    return round((j["captured_amount"] or 0) - (j["refunded_amount"] or 0) - pending, 2)


def propose_refund(job_id: str, amount: float, category: str, reasoning: str, customer_message: str = "", actor: str = "agent") -> dict:
    j = _job(job_id)
    pol = config.policy()
    amount = round(float(amount), 2)
    if not j["capture_id"]:
        _blocked(job_id, "refund.propose", "nothing captured yet; an uncaptured authorization is voided, not refunded", actor)
    room = refundable(j)
    if amount <= 0 or amount > room or amount > pol["max_refund_amount"]:
        _blocked(job_id, "refund.propose", f"refund ${amount:.2f} outside the refundable ${room:.2f}", actor, requested=amount)
    aid = db.new_id("apr")
    prop = {"category": category, "reasoning": reasoning, "customer_message": customer_message,
            "policy": pol["refund_policy"].get(category), "captured": j["captured_amount"], "already_refunded": j["refunded_amount"]}
    db.ex("INSERT INTO approvals (id, created_at, job_id, kind, amount, reason, proposal_json, status) VALUES (?,?,?,?,?,?,?,?)",
          (aid, db.now_iso(), job_id, "refund", amount, reasoning, json.dumps(prop), "pending"))
    db.ledger_append("refund.propose", "pending_approval", job_id=job_id, amount=amount, actor=actor, detail={"approval_id": aid, "category": category})
    return {"approval_id": aid, "amount": amount, "status": "pending_human_approval"}


def execute_refund(approval_id: str, approved_by: str) -> dict:
    a = db.approval(approval_id)
    j = _job(a["job_id"])
    rid = f"wrenly-{approval_id}-refund"
    if prev := _once("refund.send", rid, j["id"], approved_by):
        return {"refund_id": prev["paypal_id"], "deduped": True}
    if a["amount"] > round((j["captured_amount"] or 0) - (j["refunded_amount"] or 0), 2) + 0.001:
        _blocked(j["id"], "refund.send", "refund exceeds what is left on the capture", approved_by)
    note = "Wrenly Home: refund for your cleaning. We're sorry it wasn't right."
    try:
        r = _pp().refund_capture(j["capture_id"], a["amount"], note, rid)
    except PayPalError as e:
        db.ledger_append("refund.send", "error", job_id=j["id"], amount=a["amount"], request_id=rid, actor=approved_by, detail={"error": str(e), "debug_id": e.debug_id})
        raise
    new_ref = round((j["refunded_amount"] or 0) + a["amount"], 2)
    m = margin_rule.check(j["list_price"], j["captured_amount"] or 0, new_ref, j["extra_hours"] or 0)
    db.job_update(j["id"], margin_json=json.dumps(m))
    db.job_update(j["id"], refunded_amount=new_ref,
                  status="refunded" if new_ref >= (j["captured_amount"] or 0) - 0.001 else j["status"])
    db.ledger_append("refund.send", "ok", job_id=j["id"], amount=a["amount"], paypal_id=r["id"], request_id=rid, actor=approved_by,
                     detail={"capture_id": j["capture_id"], "paypal_status": r.get("status"), "approval_id": approval_id})
    return {"refund_id": r["id"], "status": r.get("status"), "amount": a["amount"]}


# ---- approvals ----------------------------------------------------------

def decide(approval_id: str, approve: bool, decided_by: str = "ops") -> dict:
    a = db.approval(approval_id)
    if not a:
        raise GuardrailError("no such approval")
    if a["status"] != "pending":
        return {"status": a["status"], "result": a["result"], "deduped": True}
    if not decided_by or decided_by == "agent":
        _blocked(a["job_id"], "approval.decide", "approvals must be decided by a person, not the agent", decided_by or "agent")
    result = None
    if approve:
        if a["kind"] == "refund":
            result = execute_refund(approval_id, decided_by)
        elif a["kind"] == "payout":
            result = send_payout(a["job_id"], actor=decided_by, approved_by=decided_by)
    status = "approved" if approve else "rejected"
    db.ex("UPDATE approvals SET status=?, decided_at=?, decided_by=?, result_json=? WHERE id=?",
          (status, db.now_iso(), decided_by, json.dumps(result), approval_id))
    db.ledger_append(f"approval.{status}", "ok", job_id=a["job_id"], amount=a["amount"], actor=decided_by,
                     detail={"approval_id": approval_id, "kind": a["kind"]})
    if not approve and a["kind"] == "payout":
        db.job_update(a["job_id"], status="captured")
    return {"status": status, "result": result}
