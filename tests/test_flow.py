import json
import sqlite3

import pytest

from app import config, db, money
from conftest import book, full_checklist


def test_happy_path_order_authorize_capture_payout(api, fresh):
    _, jid = book(api)
    j = db.job(jid)
    assert j["status"] == "authorized" and j["order_id"].startswith("MOCKORD") and j["authorization_id"].startswith("MOCKAUTH")
    assert fresh.money_moves == []  # authorizing moves no money

    r = api.post(f"/api/jobs/{jid}/complete", json={"checklist": full_checklist()}).json()
    j = r["job"]
    assert j["status"] == "paid_out"
    assert j["capture_id"].startswith("MOCKCAP") and j["payout_batch_id"].startswith("MOCKBATCH")
    assert j["captured_amount"] == 179 and j["payout_amount"] == 125.30
    assert [t["tool"] for t in r["steps"]] == ["capture_payment", "check_margin", "send_crew_payout"]
    assert fresh.money_moves == [("capture", 179.0), ("payout", 125.30)]
    actions = [(r["action"], r["status"]) for r in reversed(db.ledger_rows(jid))]
    assert actions == [("job.create", "ok"), ("order.create", "ok"), ("order.authorize", "ok"), ("job.complete", "ok"),
                       ("payment.capture", "ok"), ("margin.check", "pass"), ("payout.send", "ok")]


def test_paypal_request_ids_are_deterministic(api, fresh):
    _, jid = book(api)
    api.post(f"/api/jobs/{jid}/complete", json={"checklist": full_checklist()})
    ops = dict(fresh.calls)
    assert ops["create_order"] == f"wrenly-{jid}-order"
    assert ops["authorize_order"] == f"wrenly-{jid}-authorize"
    assert ops["capture"] == f"wrenly-{jid}-capture"
    assert ops["payout"] == f"wrenly-{jid}-payout"


def test_payout_retry_is_deduplicated(api, fresh):
    _, jid = book(api)
    api.post(f"/api/jobs/{jid}/complete", json={"checklist": full_checklist()})
    first = db.job(jid)["payout_batch_id"]
    for _ in range(3):
        r = api.post(f"/api/jobs/{jid}/payout").json()
        assert r["deduped"] is True and r["payout_batch_id"] == first
    assert [m for m in fresh.money_moves if m[0] == "payout"] == [("payout", 125.30)]
    assert sum(1 for r in db.ledger_rows(jid) if r["action"] == "payout.send" and r["status"] == "deduped") == 3


def test_settle_twice_never_double_charges(api, fresh):
    _, jid = book(api)
    api.post(f"/api/jobs/{jid}/complete", json={"checklist": full_checklist()})
    api.post(f"/api/jobs/{jid}/settle")
    money.capture(jid)
    assert [m for m in fresh.money_moves if m[0] == "capture"] == [("capture", 179.0)]


def test_retry_after_network_failure_uses_same_request_id(api, fresh):
    _, jid = book(api)
    money.mark_done(jid, full_checklist())
    fresh.fail_next = "capture"
    with pytest.raises(Exception):
        money.capture(jid)
    assert db.job(jid)["capture_id"] is None
    money.capture(jid)
    caps = [rid for op, rid in fresh.calls if op == "capture"]
    assert caps == [f"wrenly-{jid}-capture"] * 2  # same PayPal-Request-Id on the retry
    assert [m for m in fresh.money_moves if m[0] == "capture"] == [("capture", 179.0)]


def test_mock_replays_request_id_like_paypal(fresh):
    o = fresh.create_order(50, "x", "j", "rid-1")
    o2 = fresh.create_order(50, "x", "j", "rid-1")
    assert o["id"] == o2["id"] and len(fresh.orders) == 1


def test_duplicate_sender_batch_id_rejected_by_mock(fresh):
    fresh.create_payout("batch-1", "a@b.c", 10, "n", "i")
    fresh._replay.clear()
    with pytest.raises(Exception, match="already exists"):
        fresh.create_payout("batch-1", "a@b.c", 10, "n", "i")


def test_margin_fail_holds_payout_then_human_approves(api, fresh):
    _, jid = book(api)
    r = api.post(f"/api/jobs/{jid}/complete", json={"checklist": full_checklist(), "extra_hours": 2}).json()
    assert r["job"]["status"] == "payout_held"
    assert not any(m[0] == "payout" for m in fresh.money_moves)
    # the agent (or anyone) pressing pay is refused while held
    assert api.post(f"/api/jobs/{jid}/payout").status_code == 409
    appr = api.get("/api/approvals?status=pending").json()
    assert len(appr) == 1 and appr[0]["kind"] == "payout"
    d = api.post(f"/api/approvals/{appr[0]['id']}", json={"approve": True, "by": "Landon"}).json()
    assert d["status"] == "approved" and d["result"]["amount"] == round(179 * 0.7 + 2 * 28, 2)
    assert db.job(jid)["status"] == "paid_out"
    payout_row = next(r for r in db.ledger_rows(jid) if r["action"] == "payout.send" and r["status"] == "ok")
    assert payout_row["detail"]["approved_by"] == "Landon"


def test_guardrail_no_capture_before_completion(api, fresh):
    _, jid = book(api)
    with pytest.raises(money.GuardrailError):
        money.capture(jid)
    assert fresh.money_moves == []
    assert db.ledger_rows(jid)[0]["status"] == "blocked"


def test_guardrail_incomplete_checklist_blocks_completion(api, fresh):
    _, jid = book(api)
    cl = full_checklist()
    cl["Bathrooms scrubbed"] = False
    r = api.post(f"/api/jobs/{jid}/complete", json={"checklist": cl})
    assert r.status_code == 409 and "Bathrooms scrubbed" in r.json()["error"]
    assert db.job(jid)["status"] == "authorized" and fresh.money_moves == []


def test_guardrail_no_payout_before_capture(api, fresh):
    _, jid = book(api)
    with pytest.raises(money.GuardrailError, match="before the customer's payment is captured"):
        money.send_payout(jid)


def test_guardrail_order_cap(api):
    from app import pricing
    jid = db.job_create(customer_name="x", list_price=config.policy()["max_order_amount"] + 1, bedrooms=5, frequency="one_time",
                        quote_json=json.dumps(pricing.build_quote(5, 3, "one_time").to_dict()))
    with pytest.raises(money.GuardrailError, match="outside"):
        money.create_order(jid)


def test_authorize_rejects_foreign_order(api):
    _, jid = book(api)
    with pytest.raises(money.GuardrailError):
        money.authorize(jid, "SOMEONE-ELSES-ORDER")


def test_refund_proposed_by_agent_needs_human(api, fresh):
    cid, jid = book(api)
    api.post(f"/api/jobs/{jid}/complete", json={"checklist": full_checklist()})
    r = api.post("/api/chat", json={"message": "The crew missed the upstairs bathroom completely", "chat_id": cid}).json()
    ev = next(e for e in r["events"] if e["type"] == "refund_proposed")
    assert ev["category"] == "missed_area" and ev["amount"] == round(179 * 0.15, 2)
    assert not any(m[0] == "refund" for m in fresh.money_moves)  # proposing moves no money
    # the agent can never approve its own proposal
    with pytest.raises(money.GuardrailError):
        money.decide(ev["approval_id"], True, decided_by="agent")
    d = api.post(f"/api/approvals/{ev['approval_id']}", json={"approve": True, "by": "Landon"}).json()
    assert d["result"]["refund_id"].startswith("MOCKREF")
    assert fresh.money_moves[-1] == ("refund", 26.85)
    assert db.job(jid)["refunded_amount"] == 26.85
    # approving again is a no-op
    again = api.post(f"/api/approvals/{ev['approval_id']}", json={"approve": True, "by": "Landon"}).json()
    assert again["deduped"] and sum(1 for m in fresh.money_moves if m[0] == "refund") == 1


def test_refund_cannot_exceed_captured(api):
    _, jid = book(api)
    api.post(f"/api/jobs/{jid}/complete", json={"checklist": full_checklist()})
    money.propose_refund(jid, 150, "quality", "x")
    with pytest.raises(money.GuardrailError, match="refundable"):
        money.propose_refund(jid, 50, "quality", "pending refunds count against the capture")


def test_refund_rejected_moves_nothing(api, fresh):
    _, jid = book(api)
    api.post(f"/api/jobs/{jid}/complete", json={"checklist": full_checklist()})
    p = money.propose_refund(jid, 20, "late", "crew late")
    api.post(f"/api/approvals/{p['approval_id']}", json={"approve": False, "by": "Landon"})
    assert not any(m[0] == "refund" for m in fresh.money_moves)


def test_void_uncaptured(api, fresh):
    _, jid = book(api)
    assert api.post(f"/api/jobs/{jid}/void").json()["voided"]
    assert db.job(jid)["status"] == "cancelled"
    assert fresh.auths[db.job(jid)["authorization_id"]]["status"] == "VOIDED"


def test_ledger_is_append_only_and_mirrored(api):
    _, jid = book(api)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.ex("UPDATE ledger SET amount=0")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.ex("DELETE FROM ledger")
    lines = config.ledger_jsonl_path().read_text().strip().splitlines()
    assert len(lines) == len(db.ledger_rows()) and json.loads(lines[1])["action"] == "order.create"
    assert all(r["paypal_id"] for r in db.ledger_rows() if r["action"].startswith(("order.", "payment.", "payout.send")) and r["status"] == "ok")


def test_webhook_verification_and_dedupe(api):
    _, jid = book(api)
    ev = {"id": "WH-1", "event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": {"id": "CAP1", "custom_id": jid}}
    bad = api.post("/api/webhooks/paypal", json=ev, headers={"paypal-transmission-sig": "forged"})
    assert bad.status_code == 400
    ev["id"] = "WH-2"
    ok = api.post("/api/webhooks/paypal", json=ev, headers={"paypal-transmission-sig": "mock-valid"}).json()
    assert ok["verification"] == "verified"
    dup = api.post("/api/webhooks/paypal", json=ev, headers={"paypal-transmission-sig": "mock-valid"}).json()
    assert dup["duplicate"]


def test_summary_and_pages(api):
    _, jid = book(api)
    api.post(f"/api/jobs/{jid}/complete", json={"checklist": full_checklist()})
    s = api.get("/api/summary").json()
    assert s["captured_total"] == 179 and s["payouts_total"] == 125.30 and s["margin_total"] > 15
    for p in ("/", "/ops", f"/crew/{jid}", "/static/app.css", "/api/config"):
        assert api.get(p).status_code == 200


def test_card_payment_source_authorizes_inline(fresh):
    """The API-only sandbox path: a sandbox test card authorizes inside the create-order call."""
    from app import pricing
    q = pricing.build_quote(3, 2, "biweekly")
    jid = db.job_create(customer_name="Card", list_price=q.total, bedrooms=3, frequency="biweekly",
                        quote_json=json.dumps(q.to_dict()), crew_email="crew@example.com")
    o = money.create_order(jid, payment_source={"card": {"number": "4111111111111111", "expiry": "2030-12"}})
    j = db.job(jid)
    assert j["status"] == "authorized" and j["authorization_id"].startswith("MOCKAUTH") and o["order_id"] == j["order_id"]
