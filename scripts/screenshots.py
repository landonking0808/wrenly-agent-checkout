#!/usr/bin/env python3
"""Drive the real UI headless and save desktop screenshots to docs/screenshots/.

  python scripts/screenshots.py --url http://localhost:8000              (server running, mock PayPal)
  python scripts/screenshots.py --url ... --phase booking | ops          (run the halves separately)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent.parent / "docs" / "screenshots"
ERRORS: list[str] = []


def watch(page, name):
    page.on("console", lambda m: ERRORS.append(f"{name} console: {m.text}") if m.type == "error" else None)
    page.on("pageerror", lambda e: ERRORS.append(f"{name} pageerror: {e}"))
    return page


def wait_reply(page, n_bots: int):
    page.wait_for_function(f"document.querySelectorAll('.msg.bot').length >= {n_bots} && !document.querySelector('.msg.bot:last-child .muted:first-child')", timeout=180000)


def booking_phase(ctx, url):
    page = watch(ctx.new_page(), "book")
    page.goto(url + "/")
    page.wait_for_selector(".msg.bot")
    page.fill("#input", "Hi! We have a 3 bedroom, 2 bath house in Peoria and want it cleaned every two weeks.")
    page.click("#send")
    page.wait_for_selector(".total", timeout=180000)
    wait_reply(page, 2)
    page.screenshot(path=str(OUT / "1-booking-quote.png"))

    page.fill("#input", "Yes, book it for Friday. I'm Jamie Rivera, jamie@example.com")
    page.click("#send")
    page.wait_for_selector("#mockpp, #pp-buttons", timeout=180000)
    page.wait_for_timeout(400)
    page.screenshot(path=str(OUT / "2-booking-checkout.png"))
    page.click("#mockpp")
    page.wait_for_selector("text=Payment authorized", timeout=60000)
    page.wait_for_timeout(500)
    page.screenshot(path=str(OUT / "3-booking-authorized.png"))
    job1 = page.evaluate("JOB")

    crew = watch(ctx.new_page(), "crew")
    crew.goto(f"{url}/crew/{job1}")
    crew.wait_for_selector("#list input")
    for cb in crew.query_selector_all("#list input"):
        cb.check()
    crew.click("#done")
    crew.wait_for_selector("#result:not([hidden])", timeout=240000)
    crew.wait_for_timeout(300)
    crew.screenshot(path=str(OUT / "4-crew-settlement.png"), full_page=True)

    n = page.evaluate("document.querySelectorAll('.msg.bot').length")
    page.fill("#input", "The crew missed the upstairs bathroom completely.")
    page.click("#send")
    wait_reply(page, n + 1)
    page.wait_for_timeout(3500)  # status poll refresh
    page.screenshot(path=str(OUT / "5-booking-refund-proposed.png"))
    return job1


def ops_phase(ctx, url, api):
    cfg = api.get("/api/config").json()
    cl = {c: True for c in cfg["checklist"]}
    job1 = next(j["id"] for j in api.get("/api/jobs").json() if j["status"] in ("paid_out", "refunded"))

    def booked(text):
        r = api.post("/api/chat", json={"message": text}).json()
        r = api.post("/api/chat", json={"message": "yes please book it", "chat_id": r["chat_id"]}).json()
        ev = next(e for e in r["events"] if e["type"] == "checkout")
        api.post(f"/api/mock/approve/{ev['order_id']}")
        api.post(f"/api/orders/{ev['order_id']}/authorize", json={"job_id": ev["job_id"]})
        return ev["job_id"]

    job2 = booked("2 bedroom apartment, 1 bath, every two weeks")
    api.post(f"/api/jobs/{job2}/payout")  # refused: not captured -> shows a blocked guardrail row
    api.post(f"/api/jobs/{job2}/complete", json={"checklist": cl, "extra_hours": 1})
    booked("4 bed 3 bath one-time clean, inside the oven and fridge")  # stays authorized

    ops = watch(ctx.new_page(), "ops")
    ops.goto(url + "/ops")
    ops.wait_for_selector("tr.click")
    ops.wait_for_timeout(600)
    ops.screenshot(path=str(OUT / "6-ops-dashboard.png"))
    ops.screenshot(path=str(OUT / "6b-ops-dashboard-full.png"), full_page=True)
    ops.click(f"tr[data-id='{job2}']")
    ops.wait_for_selector(".drawer.on .bar")
    ops.wait_for_timeout(400)
    ops.screenshot(path=str(OUT / "7-ops-job-margin-held.png"))
    ops.click("#close")
    ops.click(f"tr[data-id='{job1}']")
    ops.wait_for_selector(".drawer.on .bar")
    ops.click("#retry")
    ops.wait_for_selector(".toast.on")
    ops.wait_for_timeout(1500)
    ops.screenshot(path=str(OUT / "8-ops-payout-retry-deduped.png"))
    ops.click("#close")
    while ops.query_selector("[data-approve]"):
        ops.click("[data-approve]")
        ops.wait_for_timeout(3500)
    ops.screenshot(path=str(OUT / "9-ops-after-approvals.png"), full_page=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--phase", choices=["all", "booking", "ops"], default="all")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    api = httpx.Client(base_url=a.url, timeout=240)
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 1440, "height": 900})
        if a.phase in ("all", "booking"):
            booking_phase(ctx, a.url)
        if a.phase in ("all", "ops"):
            ops_phase(ctx, a.url, api)
        m = b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True)
        mp = watch(m.new_page(), "mobile")
        mp.goto(a.url + "/ops" if a.phase == "ops" else a.url + "/")
        mp.wait_for_timeout(1500)
        mp.screenshot(path=str(OUT / ("11-ops-mobile.png" if a.phase == "ops" else "10-booking-mobile.png")))
        b.close()
    if ERRORS:
        print("\n".join(ERRORS))
        sys.exit(1)
    print(f"ok: screenshots in {OUT}")


if __name__ == "__main__":
    main()
