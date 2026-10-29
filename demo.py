#!/usr/bin/env python3
"""Scripted end-to-end demo of Wrenly Agent Checkout, for a clean screen recording.

  python demo.py                 # drive a running server at http://localhost:8000 (open /ops next to it)
  python demo.py --pause 4       # slower, for video
  python demo.py --inproc        # no server needed: runs in-process with mock PayPal
  python demo.py --fresh         # archive the old demo database first (mock mode only)

With real PayPal sandbox keys, the script prints the PayPal approval link and waits for the
sandbox buyer to approve it (or approve from the booking page) before it continues.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import webbrowser

B, D, G, Y, R, C, X = "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[36m", "\033[0m"
if not sys.stdout.isatty():
    B = D = G = Y = R = C = X = ""


def say(title: str, body: str = "") -> None:
    print(f"\n{B}{C}▸ {title}{X}")
    if body:
        print("  " + body.replace("\n", "\n  "))


def step(pause: float) -> None:
    if pause:
        time.sleep(pause)


def money(v) -> str:
    v = float(v or 0)
    return f"{'-' if v < 0 else ''}${abs(v):,.2f}"


def chat(api, text: str, chat_id=None):
    print(f"  {B}Customer:{X} {text}")
    r = api.post("/api/chat", json={"message": text, "chat_id": chat_id})
    r.raise_for_status()
    d = r.json()
    for ev in d["events"]:
        if ev["type"] == "tool":
            res = ev["result"]
            short = res.get("error") or (money(res["total"]) if "total" in res else res.get("order_id") or res.get("approval_id") or "ok")
            args = ", ".join(f"{k}={v}" for k, v in (ev["args"] or {}).items() if v not in (None, "", []))
            print(f"  {D}tool call → {ev['name']}({args}) = {short}{X}")
    print(f"  {B}Agent ({d['engine']}):{X} {d['reply']}")
    return d


def wait_for_approval(api, order_id: str, pause: float) -> None:
    o = api.get(f"/api/orders/{order_id}").json()
    if o["status"] == "APPROVED":
        return
    say("Approve in PayPal (sandbox)", f"Open this link and log in as the sandbox buyer:\n{o.get('approve_url')}")
    try:
        webbrowser.open(o.get("approve_url") or "")
    except Exception:
        pass
    while True:
        time.sleep(3)
        o = api.get(f"/api/orders/{order_id}").json()
        if o["status"] in ("APPROVED", "COMPLETED"):
            print(f"  {G}buyer approved{X}")
            return


def book(api, text: str, follow: str, mock: bool, pause: float):
    d = chat(api, text)
    step(pause)
    d = chat(api, follow, d["chat_id"])
    ev = next(e for e in d["events"] if e["type"] == "checkout")
    print(f"  {Y}PayPal Orders v2: order {ev['order_id']} (intent AUTHORIZE, {money(ev['amount'])}){X}")
    step(pause)
    if mock:
        api.post(f"/api/mock/approve/{ev['order_id']}")
        print(f"  {D}(mock) sandbox buyer clicks Pay with PayPal{X}")
    else:
        wait_for_approval(api, ev["order_id"], pause)
    jr = api.get(f"/api/jobs/{ev['job_id']}").json()
    if jr["status"] != "authorized":
        a = api.post(f"/api/orders/{ev['order_id']}/authorize", json={"job_id": ev["job_id"]})
        a.raise_for_status()
        auth = a.json()["authorization_id"]
    else:
        auth = jr["authorization_id"]
    print(f"  {G}Authorized: {auth}. Money is held on the buyer's PayPal, not charged.{X}")
    return d["chat_id"], ev["job_id"]


def complete(api, job_id: str, extra_hours: float = 0):
    cfg = api.get("/api/config").json()
    checklist = {c: True for c in cfg["checklist"]}
    print(f"  {B}Crew:{X} checks off {len(checklist)}/{len(checklist)} items" + (f", logs {extra_hours}h extra" if extra_hours else "") + " → Mark job done")
    r = api.post(f"/api/jobs/{job_id}/complete", json={"checklist": checklist, "extra_hours": extra_hours})
    r.raise_for_status()
    d = r.json()
    print(f"  {D}settlement agent engine: {d['engine']}{X}")
    for s in d["steps"]:
        o = s["result"]
        if o.get("error"):
            line = f"{R}refused: {o['error']}{X}"
        elif s["tool"] == "get_job":
            line = f"job {o['id']} · {money(o['list_price'])} · {o['status']}"
        elif s["tool"] == "capture_payment":
            line = f"captured {money(o['amount'])} · capture {o['capture_id']}"
        elif "margin" in o:
            line = f"crew {money(o['crew_pay'])} + fees {money(o['paypal_fee_est'] + o['payout_fee_est'])} → Wrenly keeps {money(o['margin'])} (floor {money(o['floor'])}) " + (f"{G}PASS{X}" if o["ok"] else f"{R}FAIL{X}")
        elif "payout_batch_id" in o:
            line = f"{G}paid crew {money(o['amount'])} · payout batch {o['payout_batch_id']}{X}"
        elif "approval_id" in o:
            line = f"{Y}held for a human · approval {o['approval_id']}{X}"
        else:
            line = str(o)
        print(f"  {D}tool call → {s['tool']}{X}  {line}")
    print(f"  {B}Agent:{X} {d['summary']}")
    return d


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("WRENLY_URL", "http://localhost:8000"))
    ap.add_argument("--pause", type=float, default=1.5)
    ap.add_argument("--inproc", action="store_true", help="run without a server (mock PayPal)")
    ap.add_argument("--fresh", action="store_true", help="archive the demo database first (mock mode)")
    a = ap.parse_args()

    if a.inproc:
        os.environ.setdefault("MOCK_PAYPAL", "1")
        os.environ.setdefault("WRENLY_DB", "data/demo-inproc.db")
        from fastapi.testclient import TestClient
        from app.main import app
        api = TestClient(app)
    else:
        import httpx
        api = httpx.Client(base_url=a.url, timeout=120)
        try:
            api.get("/healthz").raise_for_status()
        except Exception:
            sys.exit(f"No server at {a.url}. Start it with ./run.sh, or use: python demo.py --inproc")
    if a.fresh:
        api.post("/api/reset")
    cfg = api.get("/api/config").json()
    mock = cfg["paypal_mode"] == "mock"
    p = a.pause

    print(f"{B}Wrenly Agent Checkout · end-to-end demo{X}")
    print(f"{D}PayPal: {cfg['paypal_mode']} · AI: {cfg['ai']} · SANDBOX DEMO, no real money. Wrenly Home has not completed a paid job yet.{X}")

    say("1. A customer describes their home; the agent quotes it with a tool call")
    chat_id, job1 = book(api, "Hi! We have a 3 bedroom, 2 bath house in Peoria and want it cleaned every two weeks.",
                         "Yes, book it for Friday. I'm Jamie Rivera, jamie@example.com", mock, p)
    step(p)

    say("2. The crew finishes; the settlement agent captures, checks margin, then pays the crew")
    complete(api, job1)
    step(p)

    say("3. Someone presses 'pay crew' again (a retry). Idempotency: nothing is paid twice")
    r = api.post(f"/api/jobs/{job1}/payout").json()
    print(f"  {G}deduplicated={r.get('deduped')} · same batch {r.get('payout_batch_id')} · no call reached PayPal{X}")
    step(p)

    say("4. A second job. Paying the crew before the clean is done is refused by the guardrails")
    _, job2 = book(api, "2 bedroom apartment, one bath, every two weeks please", "sounds good, book it", mock, p)
    r = api.post(f"/api/jobs/{job2}/payout")
    print(f"  {R}pay crew now → HTTP {r.status_code}: {r.json().get('error')}{X}")
    step(p)
    say("   The crew ran an hour over: the margin check fails, so the payout is held for a person")
    complete(api, job2, extra_hours=1)
    step(p)
    held = [x for x in api.get("/api/approvals?status=pending").json() if x["job_id"] == job2]
    if held:
        say("   A manager reviews it in the ops dashboard and approves")
        d = api.post(f"/api/approvals/{held[0]['id']}", json={"approve": True, "by": "Landon (ops)"}).json()
        print(f"  {G}approved by Landon (ops) → payout batch {d['result']['payout_batch_id']} ({money(d['result']['amount'])}){X}")
    step(p)

    say("5. The first customer reports a problem; the agent proposes a refund, a person approves it")
    d = chat(api, "The crew missed the upstairs bathroom completely.", chat_id)
    prop = next((e for e in d["events"] if e["type"] == "refund_proposed"), None)
    step(p)
    if prop:
        r = api.post(f"/api/approvals/{prop['approval_id']}", json={"approve": True, "by": "Landon (ops)"}).json()
        print(f"  {G}refund approved → PayPal refund {r['result']['refund_id']} ({money(r['result']['amount'])}){X}")
    step(p)

    say("6. The ledger: every money action, with PayPal ids and PayPal-Request-Ids")
    for row in reversed(api.get("/api/ledger?limit=40").json()):
        if row["job_id"] not in (job1, job2):
            continue
        amt = money(row["amount"]) if row["amount"] is not None else ""
        print(f"  {D}#{row['seq']:<4}{X} {row['action']:<18} {row['status']:<17} {amt:>9}  {row['paypal_id'] or '':<20} {D}{row['request_id'] or ''}{X}")
    s = api.get("/api/summary").json()
    print(f"\n{B}Captured {money(s['captured_total'])} · paid crews {money(s['payouts_total'])} · refunded {money(s['refunded_total'])} · "
          f"{s['deduped']} duplicate(s) stopped · {s['blocked']} action(s) blocked by guardrails{X}")
    print(f"{D}Open {a.url}/ops to see it all in the dashboard.{X}")


if __name__ == "__main__":
    main()
