#!/usr/bin/env python3
"""Check headless that the checkout renders PayPal's real JS SDK buttons in mock mode (client-id=sb).

  python scripts/sb_button_check.py --url http://localhost:8000 [--screenshot out.png] [--popup]

Books a clean through the chat, waits for PayPal's button iframe inside #pp-sb, saves a screenshot,
and with --popup clicks the PayPal button and reports where PayPal's popup lands (it is closed again;
nothing is typed into it). Then it clicks the mock demo button and checks the authorization still works.
Needs internet for paypal.com; server-side PayPal calls stay on the local mock.
"""
from __future__ import annotations

import argparse
import json
import sys

from playwright.sync_api import sync_playwright


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--screenshot", default="sb-button.png")
    ap.add_argument("--popup", action="store_true", help="click the PayPal button and report the popup page")
    a = ap.parse_args()
    out: dict = {"url": a.url}
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        errs: list[str] = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.goto(a.url + "/")
        page.wait_for_selector(".msg.bot")
        page.fill("#input", "3 bedroom, 2 bath house, every two weeks")
        page.click("#send")
        page.wait_for_selector(".total", timeout=180000)
        page.fill("#input", "Yes, book it for Friday. I'm Jamie Rivera, jamie@example.com")
        page.click("#send")
        page.wait_for_selector("#mockpp", timeout=180000)
        page.wait_for_function("document.querySelector('#pp-sb') && document.querySelector('#pp-sb').dataset.state !== 'loading'", timeout=30000)
        out["sb_state"] = page.eval_on_selector("#pp-sb", "e => e.dataset.state")
        out["sdk_src"] = page.evaluate("[...document.scripts].map(s => s.src).find(s => s.includes('paypal.com/sdk/js')) || null")
        frames = [f.url for f in page.frames if "paypal.com" in f.url]
        out["paypal_iframes"] = len(frames)
        out["paypal_iframe_hosts"] = sorted({f.split("/")[2] for f in frames})
        if out["sb_state"] == "rendered":
            fr = page.frame_locator("#pp-sb iframe").first
            fr.locator("[role=link]").first.wait_for(timeout=20000)
            out["buttons"] = fr.locator("[role=link]").evaluate_all("els => els.map(e => e.getAttribute('aria-label') || e.innerText)")
        page.wait_for_timeout(800)
        page.locator("#paypal-area").scroll_into_view_if_needed()
        page.screenshot(path=a.screenshot)
        if a.popup and out["sb_state"] == "rendered":
            with ctx.expect_page(timeout=20000) as pi:
                page.frame_locator("#pp-sb iframe").first.locator("[data-funding-source=paypal]").first.click()
            pop = pi.value
            pop.wait_for_timeout(10000)
            out["popup_url"] = pop.url.split("?")[0]
            out["popup_text"] = " | ".join(pop.inner_text("body").split("\n")[:6])
            pop.close()
            page.wait_for_timeout(2000)
        page.click("#mockpp")
        page.wait_for_selector("text=Payment authorized", timeout=60000)
        out["mock_authorized"] = True
        out["page_errors"] = errs
        b.close()
    print(json.dumps(out, indent=2))
    return 0 if out.get("sb_state") in ("rendered", "unavailable") and out.get("mock_authorized") else 1


if __name__ == "__main__":
    sys.exit(main())
