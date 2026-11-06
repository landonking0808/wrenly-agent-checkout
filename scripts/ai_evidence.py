#!/usr/bin/env python3
"""Record a real Gemini run of both agents through the app's own HTTP API (PayPal in mock mode).

  GEMINI_API_KEY=... .venv/bin/python scripts/ai_evidence.py --out docs/evidence/ai-run-<date>.json

It drives /api/chat (booking agent) and /api/jobs/{id}/complete (settlement agent) with FastAPI's
TestClient against a throwaway database, and records, for every Gemini call: which model answered and
which functions the model asked for. PayPal is the local mock (MOCK_PAYPAL=1): no request reaches
PayPal and no money moves. The API key is never written to the output.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["MOCK_PAYPAL"] = "1"
os.environ["WRENLY_OFFLINE_AI"] = "0"
_tmp = tempfile.mkdtemp(prefix="wrenly-ai-evidence-")
os.environ["WRENLY_DB"] = str(Path(_tmp) / "evidence.db")
os.environ["WRENLY_LEDGER_JSONL"] = str(Path(_tmp) / "ledger.jsonl")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    from fastapi.testclient import TestClient

    from app import agent, config, db, gemini, paypal
    from app.main import app

    if not config.ai_enabled():
        print("GEMINI_API_KEY is not set: nothing to record", file=sys.stderr)
        return 2
    db.reset_for_tests()
    paypal.set_client(paypal.MockPayPal())

    calls: list[dict] = []
    original = gemini.generate

    def recording_generate(contents, system, tools, temperature=0.2):
        agent_name = "settlement" if system.startswith("You are Wrenly's settlement agent") else "booking"
        content, model = original(contents, system, tools, temperature)
        calls.append({"agent": agent_name, "model": model,
                      "function_calls": [{"name": c["name"], "args": c.get("args") or {}} for c in gemini.function_calls(content)],
                      "text": gemini.text_of(content)[:600]})
        return content, model

    agent.generate = recording_generate
    api = TestClient(app)
    turns: list[dict] = []

    def chat(text, chat_id=None):
        r = api.post("/api/chat", json={"message": text, "chat_id": chat_id})
        r.raise_for_status()
        d = r.json()
        turns.append({"endpoint": "POST /api/chat", "customer": text, "engine": d["engine"], "reply": d["reply"],
                      "tool_results": [{"name": e["name"], "args": e["args"], "result": e["result"]} for e in d["events"] if e["type"] == "tool"]})
        return d

    # Booking agent: description -> quote -> checkout (Orders v2 create, intent AUTHORIZE)
    d = chat("Hi! We have a 3 bedroom, 2 bathroom house and would like a cleaning every two weeks. We have a dog.")
    cid = d["chat_id"]
    d = chat("Sounds good, please book it. I'm Jordan Demo, jordan@example.com.", cid)
    job_id = d["job_id"]
    checkout = next((e for e in d["events"] if e["type"] == "checkout"), None)
    if not checkout:
        print("booking agent did not open checkout", file=sys.stderr)
    else:
        # Customer approval happens in the PayPal window; in mock mode the mock approves it.
        api.post(f"/api/mock/approve/{checkout['order_id']}").raise_for_status()
        a = api.post(f"/api/orders/{checkout['order_id']}/authorize", json={"job_id": job_id})
        turns.append({"endpoint": "POST /api/orders/{id}/authorize", "status": a.status_code, "result": a.json()})

        # Settlement agent: crew marks done with a full checklist -> capture, margin check, payout or hold
        checklist = {k: True for k in config.policy()["checklist"]}
        s = api.post(f"/api/jobs/{job_id}/complete", json={"checklist": checklist, "extra_hours": 0})
        s.raise_for_status()
        sd = s.json()
        turns.append({"endpoint": "POST /api/jobs/{id}/complete", "engine": sd["engine"], "summary": sd["summary"],
                      "steps": sd["steps"], "job_status": sd["job"]["status"]})

        # Booking agent again: complaint -> refund proposal that needs a human
        chat("The crew skipped the guest bathroom entirely. Not happy about that.", cid)
        appr = api.get("/api/approvals", params={"status": "pending"}).json()
        turns.append({"endpoint": "GET /api/approvals?status=pending",
                      "pending": [{k: a[k] for k in ("id", "kind", "amount", "reason", "status")} for a in appr]})

    # Settlement agent on a job that fails the margin rule (crew logged 4 extra hours)
    d2 = chat("2 bed 1 bath, one time deep clean please.")
    d2 = chat("Yes, book it.", d2["chat_id"])
    ck2 = next((e for e in d2["events"] if e["type"] == "checkout"), None)
    if ck2:
        api.post(f"/api/mock/approve/{ck2['order_id']}").raise_for_status()
        api.post(f"/api/orders/{ck2['order_id']}/authorize", json={"job_id": d2["job_id"]}).raise_for_status()
        s2 = api.post(f"/api/jobs/{d2['job_id']}/complete",
                      json={"checklist": {k: True for k in config.policy()["checklist"]}, "extra_hours": 4}).json()
        turns.append({"endpoint": "POST /api/jobs/{id}/complete (4 extra hours)", "engine": s2["engine"], "summary": s2["summary"],
                      "steps": s2["steps"], "job_status": s2["job"]["status"]})

    ct = subprocess.run(["date", "+%a %b %-d %Y %-I:%M:%S %p %Z"], env={**os.environ, "TZ": "America/Chicago"},
                        capture_output=True, text=True).stdout.strip()
    out = {
        "recorded_at_ct": ct,
        "what": "Real Google Gemini function-calling run of both agents through the app's HTTP API (FastAPI TestClient).",
        "paypal": "MOCK (local MockPayPal, simulated responses; no request reached PayPal, not the sandbox, no money moved)",
        "gemini_models_configured": config.gemini_models(),
        "models_that_answered": sorted({c["model"] for c in calls}),
        "gemini_calls": len(calls),
        "model_function_calls": calls,
        "turns": turns,
        "ledger": [{k: r[k] for k in ("seq", "action", "status", "job_id", "amount", "paypal_id", "request_id", "actor")} for r in db.ledger_rows(limit=200)],
        "api_key": "not recorded",
    }
    text = json.dumps(out, indent=2, default=str)
    key = config.gemini_key()
    assert key and key not in text
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(text + "\n")
    print(json.dumps({"models": out["models_that_answered"], "engines": [t.get("engine") for t in turns if t.get("engine")],
                      "calls": [(c["agent"], c["model"], [f["name"] for f in c["function_calls"]]) for c in calls]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
