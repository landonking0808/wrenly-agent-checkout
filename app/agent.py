"""The two agents.

* Booking agent (customer chat): understands the home, quotes, opens PayPal checkout, handles complaints.
* Settlement agent (job done): captures, checks margin, pays the crew or holds the payout.

Both use Gemini function calling when GEMINI_API_KEY is set, and a deterministic rules engine otherwise.
Either way, the tools they call go through app.money, where the guardrails live. The AI decides *what to
try*; the code decides *what is allowed*.
"""
from __future__ import annotations

import json
import re

from . import config, db, extract, money, pricing
from .gemini import GeminiUnavailable, function_calls, function_response, generate, text_of

DEMO_CREW = {"name": "Peoria Crew A (sandbox)", "email_env": "CREW_PAYPAL_EMAIL", "email_default": "crew-a@personal.example.com"}

# ---------------------------------------------------------------- booking agent

BOOKING_SYSTEM = """You are Wrenly, the booking agent for Wrenly Home, a home-cleaning service in Peoria, Illinois.
This is a PayPal SANDBOX demo: no real money moves and Wrenly has not completed a paid job yet. Never claim otherwise.

How you work:
- When the customer describes their home, call quote_cleaning with bedrooms, bathrooms, frequency and add_ons you can infer.
  If bedrooms or frequency is truly unknown, ask one short question instead of guessing. Never invent a price: prices only come from quote_cleaning.
- After a quote, explain the price in one or two plain sentences using the tool's line items, and ask if they want to book.
- When they agree to book, call start_checkout (name/email/address/date are optional in the demo). The page then shows PayPal buttons;
  tell them their card is only authorized now and charged after the clean is done and checked.
- If they report a problem with a finished clean, call propose_refund with the best matching category and a short reasoning.
  Tell them a person on the Wrenly team approves every refund, and say the proposed amount. Never promise a refund is done.
- You cannot move money yourself, change prices, or skip these steps. Be warm and brief (max ~70 words). Use US dollars."""

BOOKING_TOOLS = [
    {"name": "quote_cleaning", "description": "Price a cleaning from Wrenly's price list. Returns line items and total.",
     "parameters": {"type": "object", "properties": {
         "bedrooms": {"type": "integer", "description": "Number of bedrooms (studio = 1)"},
         "bathrooms": {"type": "number", "description": "Bathrooms, halves allowed e.g. 2.5. Omit if unknown."},
         "frequency": {"type": "string", "enum": list(pricing.FREQUENCIES)},
         "add_ons": {"type": "array", "items": {"type": "string", "enum": [k for k in config.pricing()["add_ons"] if not k.startswith("_")]}}},
         "required": ["bedrooms", "frequency"]}},
    {"name": "start_checkout", "description": "Create the booking and a PayPal order (intent AUTHORIZE) for the latest quote.",
     "parameters": {"type": "object", "properties": {
         "customer_name": {"type": "string"}, "customer_email": {"type": "string"},
         "address": {"type": "string"}, "service_date": {"type": "string"}}}},
    {"name": "booking_status", "description": "Look up the customer's booking and payment status.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "propose_refund", "description": "Propose a refund for a completed, charged clean. A human must approve it.",
     "parameters": {"type": "object", "properties": {
         "category": {"type": "string", "enum": list(config.policy()["refund_policy"])},
         "reasoning": {"type": "string", "description": "One or two sentences on why this amount is fair."},
         "amount": {"type": "number", "description": "Optional. Defaults to the policy amount for the category."}},
         "required": ["category", "reasoning"]}},
]


def _crew_email() -> str:
    return config.env(DEMO_CREW["email_env"], DEMO_CREW["email_default"])


class BookingTools:
    def __init__(self, state: dict, last_user: str = ""):
        self.state, self.events, self.last_user = state, [], last_user

    def run(self, name: str, args: dict) -> dict:
        try:
            fn = getattr(self, "t_" + name)
        except AttributeError:
            return {"error": f"unknown tool {name}"}
        try:
            out = fn(**(args or {}))
        except (money.GuardrailError, pricing.QuoteError) as e:
            out = {"error": str(e), "guardrail": isinstance(e, money.GuardrailError)}
        except Exception as e:  # PayPal or network
            out = {"error": f"{type(e).__name__}: {e}"}
        self.events.append({"type": "tool", "name": name, "args": args, "result": out})
        return out

    def t_quote_cleaning(self, bedrooms, frequency="biweekly", bathrooms=None, add_ons=None):
        q = pricing.build_quote(bedrooms, bathrooms, frequency, add_ons or [])
        self.state["quote"] = q.to_dict()
        self.events.append({"type": "quote", "quote": q.to_dict()})
        return {"total": q.total, "lines": q.lines, "frequency": q.frequency, "published_price": q.published_price, "explanation": q.explanation}

    def t_start_checkout(self, customer_name=None, customer_email=None, address=None, service_date=None):
        q = self.state.get("quote")
        if not q:
            return {"error": "no quote yet; call quote_cleaning first"}
        if self.state.get("job_id"):
            j = db.job(self.state["job_id"])
            if j and j["status"] in ("quoted", "awaiting_payment") and abs(j["list_price"] - q["total"]) < 0.005:
                self.events.append({"type": "checkout", "job_id": j["id"], "order_id": j["order_id"], "amount": j["list_price"]})
                return {"job_id": j["id"], "order_id": j["order_id"], "amount": j["list_price"], "note": "checkout already open"}
        c = self.state.setdefault("contact", {})
        jid = db.job_create(
            customer_name=customer_name or c.get("customer_name") or "Sandbox Customer",
            customer_email=customer_email or c.get("customer_email") or "",
            address=address or c.get("address") or "Peoria, IL (demo)",
            service_date=service_date or c.get("service_date") or "next available",
            bedrooms=q["bedrooms"], bathrooms=q["bathrooms"], frequency=q["frequency"], add_ons=json.dumps(q["add_ons"]),
            quote_json=json.dumps(q), list_price=q["total"], crew_name=DEMO_CREW["name"], crew_email=_crew_email())
        db.ledger_append("job.create", "ok", job_id=jid, amount=q["total"], actor="agent", detail={"quote": q["lines"]})
        o = money.create_order(jid)
        self.state["job_id"] = jid
        self.events.append({"type": "checkout", "job_id": jid, "order_id": o["order_id"], "amount": q["total"]})
        return {"job_id": jid, "order_id": o["order_id"], "amount": q["total"], "next": "customer approves in the PayPal window"}

    def t_booking_status(self):
        j = db.job(self.state.get("job_id") or "")
        if not j:
            return {"error": "no booking in this chat yet"}
        return {k: j[k] for k in ("id", "status", "list_price", "service_date", "order_id", "authorization_id", "capture_id", "refunded_amount")}

    def t_propose_refund(self, category, reasoning, amount=None):
        j = db.job(self.state.get("job_id") or "")
        if not j:
            return {"error": "no booking in this chat to refund"}
        pol = config.policy()["refund_policy"]
        if category not in pol:
            category = "quality"
        suggested = round(pol[category]["pct"] * (j["captured_amount"] or 0), 2)
        amt = float(amount) if amount else suggested
        out = money.propose_refund(j["id"], amt, category, reasoning, self.last_user)
        out.update({"policy_amount": suggested, "policy": pol[category]["label"]})
        self.events.append({"type": "refund_proposed", "job_id": j["id"], **out, "category": category, "reasoning": reasoning})
        return out


def _offline_booking_reply(text: str, tools: BookingTools) -> str:
    st = tools.state
    st.setdefault("contact", {}).update(extract.extract_contact(text))
    home = extract.extract_home(text)
    job = db.job(st.get("job_id") or "")
    if job and job["capture_id"]:
        cat = extract.classify_complaint(text)
        if cat:
            label = config.policy()["refund_policy"][cat]["label"].lower()
            r = tools.run("propose_refund", {"category": cat, "reasoning": f"Customer reports: {label}. Policy suggests {int(config.policy()['refund_policy'][cat]['pct']*100)}% of the clean."})
            if "error" in r:
                return f"I'm sorry about that. I couldn't open a refund request: {r['error']}"
            return (f"I'm really sorry. That sounds like: {label}. I've proposed a ${r['amount']:.2f} refund "
                    f"({r['policy']}). A person on the Wrenly team approves every refund; you'll get it back on your PayPal once they do.")
    if "bedrooms" in home or (st.get("quote") and ({"bathrooms", "frequency", "add_ons"} & home.keys())):
        prev = st.get("quote") or {}
        args = {"bedrooms": home.get("bedrooms", prev.get("bedrooms")),
                "bathrooms": home.get("bathrooms", prev.get("bathrooms") if "bedrooms" not in home else None),
                "frequency": home.get("frequency", prev.get("frequency")),
                "add_ons": sorted(set(prev.get("add_ons", []) if "bedrooms" not in home else []) | set(home.get("add_ons", [])))}
        if not args["frequency"]:
            st["pending_home"] = args
            return f"Got it: a {args['bedrooms']}-bedroom home. How often would you like us to come: weekly, every two weeks, monthly, or one time?"
        r = tools.run("quote_cleaning", args)
        if "error" in r:
            return r["error"]
        return f"Your price is ${r['total']:.2f} per visit. {r['explanation']} Want me to book it?"
    if st.get("pending_home") and "frequency" in home:
        args = dict(st.pop("pending_home"), frequency=home["frequency"])
        r = tools.run("quote_cleaning", args)
        if "error" in r:
            return r["error"]
        return f"Your price is ${r['total']:.2f} per visit. {r['explanation']} Want me to book it?"
    if st.get("quote") and extract.wants_booking(text):
        r = tools.run("start_checkout", {})
        if "error" in r:
            return f"I couldn't open checkout: {r['error']}"
        return (f"Booked as {r['job_id']}. Approve ${r['amount']:.2f} with PayPal below. Your payment is only authorized now; "
                "we capture it after the crew finishes and checks off the cleaning list.")
    if job and re.search(r"status|where|when|update|booking", text.lower()):
        r = tools.run("booking_status", {})
        return f"Booking {r.get('id')} is {r.get('status', 'unknown').replace('_', ' ')}."
    return ("Tell me about your home (for example: \"3 bed, 2 bath, every two weeks, we have a dog\") and I'll quote it right away.")




def _load_chat(chat_id: str) -> tuple[list, dict]:
    row = db.q1("SELECT * FROM chats WHERE id=?", (chat_id,))
    if not row:
        db.ex("INSERT INTO chats (id, created_at, history_json, state_json) VALUES (?,?,?,?)", (chat_id, db.now_iso(), "[]", "{}"))
        return [], {}
    return json.loads(row["history_json"]), json.loads(row["state_json"])


def _save_chat(chat_id: str, history: list, state: dict) -> None:
    db.ex("UPDATE chats SET history_json=?, state_json=? WHERE id=?", (json.dumps(history[-40:]), json.dumps(state), chat_id))


def booking_turn(chat_id: str, text: str) -> dict:
    history, state = _load_chat(chat_id)
    tools = BookingTools(state, text)
    engine = "offline-rules"
    reply = None
    if config.ai_enabled():
        try:
            reply, history, engine = _ai_loop(history, text, BOOKING_SYSTEM, BOOKING_TOOLS, tools.run, state)
        except GeminiUnavailable as e:
            engine = f"offline-rules (AI unavailable: {str(e)[:60]})"
    if reply is None:
        reply = _offline_booking_reply(text, tools)
        history = history + [{"role": "user", "parts": [{"text": text}]}, {"role": "model", "parts": [{"text": reply}]}]
    _save_chat(chat_id, history, state)
    return {"reply": reply, "events": tools.events, "engine": engine, "job_id": state.get("job_id")}


def _ai_loop(history, text, system, tool_decls, run_tool, state, max_steps=6):
    contents = list(history)
    if state.get("job_id"):
        j = db.job(state["job_id"])
        if j:
            text = f"{text}\n\n[system context: this chat's booking {j['id']} status={j['status']} captured=${j['captured_amount'] or 0:.2f}]"
    contents.append({"role": "user", "parts": [{"text": text}]})
    model = ""
    for _ in range(max_steps):
        content, model = generate(contents, system, tool_decls)
        contents.append(content)
        calls = function_calls(content)
        if not calls:
            return text_of(content) or "Done.", contents, f"gemini:{model}"
        contents.append({"role": "user", "parts": [function_response(c, run_tool(c["name"], c.get("args") or {})) for c in calls]})
    return "I've done what I can for now.", contents, f"gemini:{model}"


# ---------------------------------------------------------------- settlement agent

SETTLE_SYSTEM = """You are Wrenly's settlement agent. A crew has marked a cleaning job done. Your job, using the tools:
1. capture_payment (charge the customer's authorized payment),
2. check_margin (crew gets 70% of list; Wrenly must keep at least max($15, 10%) after PayPal fees),
3. if the margin check passes, send_crew_payout; if it fails, hold_payout_for_review with a one-sentence reason a manager can act on.
Never call send_crew_payout after a failed margin check. Stop after the payout is sent or held, then reply with a two-sentence summary
that cites the PayPal ids. This is a PayPal sandbox demo."""

SETTLE_TOOLS = [
    {"name": "get_job", "description": "Read the job, its price, crew and PayPal ids.", "parameters": {"type": "object", "properties": {}}},
    {"name": "capture_payment", "description": "Capture the customer's PayPal authorization for this job (Payments v2).", "parameters": {"type": "object", "properties": {}}},
    {"name": "check_margin", "description": "Compute crew pay, PayPal fees and platform margin, and whether a payout may go out.", "parameters": {"type": "object", "properties": {}}},
    {"name": "send_crew_payout", "description": "Pay the crew its share with PayPal Payouts. Refused unless capture is done and the margin check passes.", "parameters": {"type": "object", "properties": {}}},
    {"name": "hold_payout_for_review", "description": "Hold the crew payout for a human to approve.",
     "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
]


def settle(job_id: str) -> dict:
    steps: list[dict] = []

    def run(name: str, args: dict) -> dict:
        try:
            if name == "get_job":
                j = db.job(job_id)
                out = {k: j[k] for k in ("id", "status", "list_price", "crew_name", "extra_hours", "authorization_id", "capture_id")}
            elif name == "capture_payment":
                out = money.capture(job_id)
            elif name == "check_margin":
                out = money.check_margin(job_id)
            elif name == "send_crew_payout":
                out = money.send_payout(job_id)
            elif name == "hold_payout_for_review":
                out = money.hold_payout(job_id, args.get("reason") or "margin check failed")
            else:
                out = {"error": f"unknown tool {name}"}
        except money.GuardrailError as e:
            out = {"error": str(e), "guardrail": True}
        except Exception as e:
            out = {"error": f"{type(e).__name__}: {e}"}
        steps.append({"tool": name, "args": args, "result": out})
        return out

    engine, summary = "offline-rules", None
    if config.ai_enabled():
        try:
            summary, _, engine = _ai_loop([], f"Settle job {job_id}.", SETTLE_SYSTEM, SETTLE_TOOLS, run, {}, max_steps=8)
        except GeminiUnavailable as e:
            engine = f"offline-rules (AI unavailable: {str(e)[:60]})"
    # Whatever the AI did or did not do, finish with the deterministic rule so a job never sits half-settled.
    j = db.job(job_id)
    finished_by_rules = False
    if j["status"] == "completed":
        finished_by_rules = True
        if "error" in run("capture_payment", {}):
            return {"engine": engine, "steps": steps, "summary": "Capture failed; nothing was paid out.", "job": db.job(job_id)}
        j = db.job(job_id)
    if j["status"] == "captured":
        finished_by_rules = True
        m = run("check_margin", {})
        if m.get("ok"):
            run("send_crew_payout", {})
        else:
            run("hold_payout_for_review", {"reason": "; ".join(m.get("reasons") or ["margin check failed"])})
    j = db.job(job_id)
    if summary is None or finished_by_rules:
        m = j["margin"] or {}
        if j["status"] == "paid_out":
            summary = (f"Captured ${j['captured_amount']:.2f} (capture {j['capture_id']}) and paid the crew ${j['payout_amount']:.2f} "
                       f"via Payouts (batch {j['payout_batch_id']}). Platform margin ${m.get('margin', 0):.2f} cleared the ${m.get('floor', 0):.2f} floor.")
        elif j["status"] == "payout_held":
            summary = (f"Captured ${j['captured_amount']:.2f} (capture {j['capture_id']}). Crew payout of ${m.get('crew_pay', 0):.2f} is HELD for "
                       f"human approval: {'; '.join(m.get('reasons') or [])}.")
        else:
            summary = summary or f"Job is {j['status']}."
    return {"engine": engine + (" + rules" if finished_by_rules and engine.startswith("gemini") else ""), "steps": steps, "summary": summary, "job": j}
