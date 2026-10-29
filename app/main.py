"""HTTP API + pages. Run: uvicorn app.main:app --port 8000"""
from __future__ import annotations

import json
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, agent, config, db, margin, money
from .paypal import MockPayPal, PayPalError, client

app = FastAPI(title="Wrenly Agent Checkout", version=__version__)
STATIC = config.ROOT / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.exception_handler(money.GuardrailError)
async def _guardrail(_: Request, e: money.GuardrailError):
    return JSONResponse({"error": str(e), "guardrail": True}, status_code=409)


@app.exception_handler(PayPalError)
async def _pp_error(_: Request, e: PayPalError):
    return JSONResponse({"error": str(e), "paypal_debug_id": e.debug_id}, status_code=502)


def _ops_auth(request: Request) -> None:
    tok = config.env("OPS_TOKEN")
    if tok and request.headers.get("x-ops-token") != tok:
        raise HTTPException(401, "ops token required")


# ---------------------------------------------------------------- pages

@app.get("/")
def page_book():
    return FileResponse(STATIC / "index.html")


@app.get("/ops")
def page_ops():
    return FileResponse(STATIC / "ops.html")


@app.get("/crew/{job_id}")
def page_crew(job_id: str):
    return FileResponse(STATIC / "crew.html")


# ---------------------------------------------------------------- api

@app.get("/api/config")
def api_config():
    pol = config.policy()
    return {
        "paypal_mode": client().mode, "paypal_client_id": "" if config.mock_paypal() else config.env("PAYPAL_CLIENT_ID"),
        "ai": f"gemini ({config.gemini_models()[0]})" if config.ai_enabled() else "offline rules",
        "currency": "USD", "checklist": pol["checklist"], "crew_share_pct": pol["crew_share_pct"], "crew_extra_hour_rate": pol["crew_extra_hour_rate"],
        "margin_floor": {"abs": pol["margin_floor_abs"], "pct": pol["margin_floor_pct"]},
        "caps": {k: pol[k] for k in ("max_order_amount", "max_payout_amount", "max_refund_amount")},
        "pricing": config.pricing(), "version": __version__,
    }


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    chat_id: str | None = None


@app.post("/api/chat")
def api_chat(body: ChatIn):
    cid = body.chat_id or "chat_" + uuid.uuid4().hex[:10]
    out = agent.booking_turn(cid, body.message)
    out["chat_id"] = cid
    return out


class AuthorizeIn(BaseModel):
    job_id: str


@app.post("/api/orders/{order_id}/authorize")
def api_authorize(order_id: str, body: AuthorizeIn):
    return money.authorize(body.job_id, order_id)


@app.get("/api/orders/{order_id}")
def api_order(order_id: str):
    o = client().get_order(order_id)
    return {"id": o["id"], "status": o.get("status"),
            "approve_url": next((l["href"] for l in o.get("links", []) if l.get("rel") in ("payer-action", "approve")), None)}


@app.post("/api/mock/approve/{order_id}")
def api_mock_approve(order_id: str):
    pp = client()
    if not isinstance(pp, MockPayPal):
        raise HTTPException(400, "only in MOCK_PAYPAL mode; use the PayPal buttons")
    pp.approve(order_id)
    return {"approved": True}


@app.get("/api/jobs")
def api_jobs():
    return [_job_view(j) for j in db.jobs()]


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    j = db.job(job_id)
    if not j:
        raise HTTPException(404, "no such job")
    return {**_job_view(j), "ledger": db.ledger_rows(job_id), "checklist_items": config.policy()["checklist"]}


def _job_view(j: dict) -> dict:
    out = {k: v for k, v in j.items() if not k.endswith("_json")}
    if not out.get("margin"):
        out["margin_preview"] = margin.check(j["list_price"], j["list_price"], j["refunded_amount"] or 0, j["extra_hours"] or 0)
    return out


class CompleteIn(BaseModel):
    checklist: dict[str, bool]
    extra_hours: float = 0.0


@app.post("/api/jobs/{job_id}/complete")
def api_complete(job_id: str, body: CompleteIn):
    money.mark_done(job_id, body.checklist, body.extra_hours)
    return agent.settle(job_id)


@app.post("/api/jobs/{job_id}/settle")
def api_settle(job_id: str, request: Request):
    _ops_auth(request)
    return agent.settle(job_id)


@app.post("/api/jobs/{job_id}/payout")
def api_payout_retry(job_id: str, request: Request):
    """Manual 'pay crew' button. Safe to press twice: the second press is deduplicated."""
    _ops_auth(request)
    return money.send_payout(job_id, actor="ops")


@app.post("/api/jobs/{job_id}/void")
def api_void(job_id: str, request: Request):
    _ops_auth(request)
    return money.void(job_id)


@app.get("/api/approvals")
def api_approvals(status: str | None = None):
    rows = db.approvals(status)
    for a in rows:
        j = db.job(a["job_id"]) or {}
        a["job"] = {k: j.get(k) for k in ("customer_name", "list_price", "captured_amount", "refunded_amount", "status", "crew_name")}
    return rows


class DecideIn(BaseModel):
    approve: bool
    by: str = Field(min_length=2, max_length=60)


@app.post("/api/approvals/{approval_id}")
def api_decide(approval_id: str, body: DecideIn, request: Request):
    _ops_auth(request)
    return money.decide(approval_id, body.approve, body.by)


@app.get("/api/ledger")
def api_ledger(limit: int = 300):
    return db.ledger_rows(limit=limit)


@app.get("/api/summary")
def api_summary():
    js = db.jobs()
    paid = [j for j in js if j["status"] == "paid_out"]
    return {
        "jobs": len(js),
        "authorized": sum(1 for j in js if j["status"] == "authorized"),
        "captured_total": round(sum(j["captured_amount"] or 0 for j in js), 2),
        "refunded_total": round(sum(j["refunded_amount"] or 0 for j in js), 2),
        "payouts_total": round(sum(j["payout_amount"] or 0 for j in js), 2),
        "margin_total": round(sum((j["margin"] or {}).get("margin", 0) for j in paid), 2),
        "pending_approvals": len(db.approvals("pending")),
        "ledger_rows": (db.q1("SELECT COUNT(*) n FROM ledger") or {"n": 0})["n"],
        "deduped": (db.q1("SELECT COUNT(*) n FROM ledger WHERE status='deduped'") or {"n": 0})["n"],
        "blocked": (db.q1("SELECT COUNT(*) n FROM ledger WHERE status='blocked'") or {"n": 0})["n"],
        "paypal_mode": client().mode,
    }


@app.post("/api/webhooks/paypal")
async def api_webhook(request: Request):
    raw = await request.body()
    try:
        event = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(400, "bad json")
    wid = config.env("PAYPAL_WEBHOOK_ID")
    verified = "skipped (no PAYPAL_WEBHOOK_ID)"
    if wid or isinstance(client(), MockPayPal):
        try:
            verified = "verified" if client().verify_webhook(dict(request.headers), event, wid) else "FAILED"
        except PayPalError as e:
            verified = f"error: {e}"
    eid = event.get("id") or "evt_" + uuid.uuid4().hex[:10]
    res = event.get("resource") or {}
    if db.q1("SELECT id FROM webhook_events WHERE id=?", (eid,)):
        return {"ok": True, "duplicate": True}
    db.ex("INSERT INTO webhook_events (id, received_at, event_type, resource_id, verified, body) VALUES (?,?,?,?,?,?)",
          (eid, db.now_iso(), event.get("event_type"), res.get("id"), verified, raw.decode()[:20000]))
    job_id = res.get("custom_id") or (res.get("invoice_id") or "").replace("wrenly-", "") or None
    db.ledger_append("webhook." + (event.get("event_type") or "unknown").lower(), "verified" if verified == "verified" else "received",
                     job_id=job_id if job_id and db.job(job_id) else None, paypal_id=res.get("id"), actor="paypal",
                     detail={"event_id": eid, "verification": verified})
    if verified == "FAILED":
        raise HTTPException(400, "webhook signature failed")
    return {"ok": True, "verification": verified}


@app.get("/api/webhooks")
def api_webhooks():
    return db.q("SELECT id, received_at, event_type, resource_id, verified FROM webhook_events ORDER BY received_at DESC LIMIT 50")


@app.post("/api/reset")
def api_reset(request: Request):
    """Demo only: start a fresh database. The old ledger is archived, never edited."""
    if not config.mock_paypal() and config.env("ALLOW_RESET") != "1":
        raise HTTPException(403, "reset is only for mock/demo mode")
    _ops_auth(request)
    p = config.db_path()
    db.reset_for_tests()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for f in (p, config.ledger_jsonl_path()):
        if f.exists():
            f.rename(f.with_name(f"{f.stem}.archived-{stamp}{f.suffix}"))
    return {"reset": True}


@app.get("/healthz")
def healthz():
    return {"ok": True, "paypal": client().mode, "ai": config.ai_enabled()}
