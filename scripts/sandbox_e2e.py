#!/usr/bin/env python3
"""Run the whole money flow against the REAL PayPal sandbox and record every PayPal id.

  PAYPAL_CLIENT_ID=... PAYPAL_CLIENT_SECRET=... CREW_PAYPAL_EMAIL=<sandbox personal email> \\
    python scripts/sandbox_e2e.py [--buyer-email ... --buyer-password ...] [--creds sandbox.json]

How the buyer approval happens, in order of preference:
  1. --approve browser: log in as the sandbox buyer in headless Chromium (Playwright) and approve the order
  2. --approve card:    pay with a PayPal sandbox test card (needs Advanced Card Processing on the sandbox app)
  3. --approve manual:  print the approval link and wait for you to approve it in a browser
Writes the ids (never secrets) to docs/sandbox-run.json.

  --preflight   only check the credentials: get an OAuth token, confirm the app has the Payouts scope,
                and that the crew receiver is not the buyer. Moves no money, creates nothing.
  --headed      run the buyer approval in a visible Chromium (e.g. under xvfb-run) if PayPal balks at headless.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def load_creds(a) -> dict:
    c = {}
    if a.creds:
        c = json.loads(Path(a.creds).read_text())
    env = os.environ
    out = {
        "client_id": env.get("PAYPAL_CLIENT_ID") or c.get("client_id"),
        "client_secret": env.get("PAYPAL_CLIENT_SECRET") or c.get("client_secret"),
        "buyer_email": a.buyer_email or c.get("buyer_email"),
        "buyer_password": a.buyer_password or c.get("buyer_password"),
        "crew_email": env.get("CREW_PAYPAL_EMAIL") or c.get("crew_email"),
    }
    if not (out["client_id"] and out["client_secret"]):
        sys.exit("Need PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET (sandbox).")
    if not out["crew_email"]:
        sys.exit("Need CREW_PAYPAL_EMAIL: a sandbox personal account that receives the crew payout.")
    return out


PAYOUTS_SCOPE = "https://uri.paypal.com/services/payments/payouts"


def preflight(cr: dict, base: str = "https://api-m.sandbox.paypal.com", http=None) -> bool:
    """Check the sandbox credentials without moving money. Returns True when everything the run needs is there."""
    import httpx
    http = http or httpx
    r = http.post(f"{base}/v1/oauth2/token", data={"grant_type": "client_credentials"},
                  auth=(cr["client_id"], cr["client_secret"]), timeout=45)
    if r.status_code != 200:
        print(f"  FAIL oauth token: HTTP {r.status_code} (wrong client id/secret, or not a sandbox app)")
        return False
    d = r.json()
    scopes = set((d.get("scope") or "").split())
    print(f"  ok   oauth token (app_id {d.get('app_id', '?')}, {len(scopes)} scopes)")
    ok = True
    if PAYOUTS_SCOPE in scopes or any("payouts" in x for x in scopes):
        print("  ok   Payouts scope present")
    else:
        print("  FAIL Payouts scope missing: enable Payouts in the sandbox app's features, then wait a few minutes")
        ok = False
    if cr.get("buyer_email") and cr["buyer_email"].strip().lower() == (cr.get("crew_email") or "").strip().lower():
        print("  FAIL crew_email is the buyer: use a second sandbox Personal account for the crew payout")
        ok = False
    if not (cr.get("buyer_email") and cr.get("buyer_password")):
        print("  warn no buyer_email/buyer_password: approval falls back to a sandbox card, then to a manual link")
    return ok


def approve_in_browser(url: str, email: str, password: str, order_id: str, pp, headed: bool = False) -> bool:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=not headed)
        page = b.new_page(viewport={"width": 1100, "height": 900})
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_selector("#email", timeout=60000)
        page.fill("#email", email)
        if page.is_visible("#btnNext"):
            page.click("#btnNext")
        page.wait_for_selector("#password", state="visible", timeout=60000)
        page.fill("#password", password)
        page.click("#btnLogin")
        for _ in range(60):
            if pp.get_order(order_id)["status"] in ("APPROVED", "COMPLETED"):
                break
            for sel in ("#payment-submit-btn", "button[data-id='payment-submit-btn']", "button:has-text('Continue to Review Order')",
                        "button:has-text('Continue')", "button:has-text('Complete Purchase')", "button:has-text('Pay')"):
                try:
                    if page.is_visible(sel):
                        page.click(sel, timeout=5000)
                        break
                except Exception:
                    pass
            time.sleep(2)
        page.screenshot(path=str(ROOT / "docs" / "screenshots" / "sandbox-buyer-approval.png"))
        b.close()
    return pp.get_order(order_id)["status"] in ("APPROVED", "COMPLETED")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--creds", help="optional JSON with client_id, client_secret, buyer_email, buyer_password, crew_email")
    ap.add_argument("--buyer-email")
    ap.add_argument("--buyer-password")
    ap.add_argument("--approve", choices=["browser", "card", "manual", "auto"], default="auto")
    ap.add_argument("--preflight", action="store_true", help="check credentials and Payouts scope only; moves no money")
    ap.add_argument("--headed", action="store_true", help="visible Chromium for the buyer approval (needs DISPLAY)")
    a = ap.parse_args()
    cr = load_creds(a)
    if a.preflight:
        print("Wrenly Agent Checkout · PayPal sandbox preflight")
        sys.exit(0 if preflight(cr) else 1)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    os.environ.update({"PAYPAL_CLIENT_ID": cr["client_id"], "PAYPAL_CLIENT_SECRET": cr["client_secret"], "PAYPAL_ENV": "sandbox",
                       "MOCK_PAYPAL": "0", "CREW_PAYPAL_EMAIL": cr["crew_email"],
                       "WRENLY_DB": str(ROOT / "data" / f"sandbox-run-{stamp}.db")})
    from app import agent, config, db, money, pricing
    from app.paypal import client

    pp = client()
    assert pp.mode == "sandbox", pp.mode
    log = {"when": stamp, "paypal_env": "sandbox", "steps": []}

    def rec(name, **kv):
        log["steps"].append({"step": name, **kv})
        print(f"  {name}: " + ", ".join(f"{k}={v}" for k, v in kv.items()))

    print("Wrenly Agent Checkout · real PayPal SANDBOX run")
    q = pricing.build_quote(3, 2, "biweekly")
    jid = db.job_create(customer_name="Sandbox Buyer", customer_email=cr.get("buyer_email") or "", address="Peoria, IL (sandbox)",
                        service_date="sandbox test", bedrooms=3, bathrooms=2, frequency="biweekly", add_ons="[]",
                        quote_json=json.dumps(q.to_dict()), list_price=q.total, crew_name="Sandbox crew", crew_email=cr["crew_email"])
    rec("quote", job_id=jid, total=q.total)

    mode = a.approve
    if mode == "auto":
        mode = "browser" if cr.get("buyer_email") and cr.get("buyer_password") else "card"
    if mode == "card":
        try:
            o = money.create_order(jid, payment_source={"card": {"number": "4032039317984658", "expiry": "2030-12", "security_code": "123",
                                                                 "name": "Sandbox Buyer", "billing_address": {"country_code": "US", "postal_code": "61602"}}})
            rec("order.create(card)", order_id=o["order_id"], status=o["status"])
        except Exception as e:
            print(f"  card path failed ({e}); falling back to manual approval")
            db.reset_for_tests()
            os.environ["WRENLY_DB"] = str(ROOT / "data" / f"sandbox-run-{stamp}-b.db")
            jid = db.job_create(customer_name="Sandbox Buyer", list_price=q.total, bedrooms=3, bathrooms=2, frequency="biweekly",
                                add_ons="[]", quote_json=json.dumps(q.to_dict()), crew_name="Sandbox crew", crew_email=cr["crew_email"])
            mode = "manual"
    if mode in ("browser", "manual"):
        o = money.create_order(jid)
        rec("order.create", order_id=o["order_id"], status=o["status"])
        url = o.get("approve_url")
        ok = False
        if mode == "browser":
            try:
                ok = approve_in_browser(url, cr["buyer_email"], cr["buyer_password"], o["order_id"], pp, headed=a.headed)
            except Exception as e:
                print(f"  browser approval failed: {e}")
        if not ok:
            print(f"\n  Approve as the sandbox buyer: {url}\n  waiting…")
            while pp.get_order(o["order_id"])["status"] not in ("APPROVED", "COMPLETED"):
                time.sleep(4)
        rec("buyer.approved", order_status=pp.get_order(o["order_id"])["status"])
        r = money.authorize(jid, o["order_id"])
        rec("order.authorize", authorization_id=r["authorization_id"], status=r.get("status"))

    money.mark_done(jid, {c: True for c in config.policy()["checklist"]})
    s = agent.settle(jid)
    j = db.job(jid)
    rec("settle", engine=s["engine"], capture_id=j["capture_id"], payout_batch_id=j["payout_batch_id"], status=j["status"])
    r = money.send_payout(jid, actor="retry-test")
    rec("payout.retry", deduped=r.get("deduped"), payout_batch_id=r["payout_batch_id"])
    try:
        pr = pp.get_payout(j["payout_batch_id"])
        rec("payout.status", batch_status=pr["batch_header"]["batch_status"],
            item_status=(pr.get("items") or [{}])[0].get("transaction_status"), payout_item_id=(pr.get("items") or [{}])[0].get("payout_item_id"))
    except Exception as e:
        rec("payout.status", error=str(e))
    p = money.propose_refund(jid, round(q.total * 0.15, 2), "missed_area", "Sandbox test: one room missed.")
    d = money.decide(p["approval_id"], True, "sandbox-test-operator")
    rec("refund", approval_id=p["approval_id"], refund_id=d["result"]["refund_id"], amount=d["result"]["amount"])
    log["ledger"] = [{k: r[k] for k in ("seq", "action", "status", "amount", "paypal_id", "request_id")} for r in reversed(db.ledger_rows(jid))]
    out = ROOT / "docs" / "sandbox-run.json"
    out.write_text(json.dumps(log, indent=2))
    print(f"\nok: PayPal sandbox ids written to {out}")


if __name__ == "__main__":
    main()
