"""Deterministic, offline understanding of customer messages. Used when no AI key is set, and in tests."""
from __future__ import annotations

import re

WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "studio": 1, "single": 1}
NUM = r"(\d+(?:\.\d+)?|a|an|one|two|three|four|five|six|single)"


def _n(tok: str) -> float:
    tok = tok.lower()
    return float(WORDS[tok]) if tok in WORDS else float(tok)


def extract_home(text: str) -> dict:
    t = text.lower().replace("-", " ")
    out: dict = {}
    if m := re.search(NUM + r"\s*(?:bed(?:room)?s?|br|bd)\b", t):
        out["bedrooms"] = int(_n(m.group(1)))
    elif "studio" in t:
        out["bedrooms"] = 1
    if m := re.search(NUM + r"\s*(and a half|1/2|½)?\s*(?:full\s+)?(?:bath(?:room)?s?|ba)\b", t):
        out["bathrooms"] = _n(m.group(1)) + (0.5 if m.group(2) else 0)
        if not m.group(2) and re.search(r"(and|plus|\+)\s*(a|one)\s+half\s*bath", t):
            out["bathrooms"] += 0.5
    if re.search(r"bi ?weekly|every (two|2|other) weeks?|every other week|twice a month|fortnight", t):
        out["frequency"] = "biweekly"
    elif re.search(r"\bweekly\b|every week|once a week", t):
        out["frequency"] = "weekly"
    elif re.search(r"monthly|once a month|every month", t):
        out["frequency"] = "monthly"
    elif re.search(r"one ?time|just once|move ?(out|in)|single clean|one off|before (a|the) party", t):
        out["frequency"] = "one_time"
    adds = []
    for key, pats in {
        "inside_fridge": r"fridge|refrigerator",
        "inside_oven": r"\boven\b",
        "interior_windows": r"window",
        "laundry": r"laundry|wash and fold|washing",
        "pets": r"\b(dog|dogs|cat|cats|pet|pets|shed)",
        "deep_clean": r"deep clean|hasn'?t been cleaned|first clean is (a )?deep|really dirty",
    }.items():
        if re.search(pats, t):
            adds.append(key)
    if adds:
        out["add_ons"] = adds
    return out


COMPLAINT = {
    "no_show": r"(didn'?t|did not|never) (show|come|arrive)|no ?show|nobody came|no one came",
    "damage": r"broke|broken|damag|scratch|stain(ed)? (my|the)|chipped",
    "missed_area": r"miss(ed)?|skipp(ed)?|forgot|didn'?t (clean|do|touch)|left (the|my) .{0,20}(dirty|untouched)",
    "late": r"\blate\b|hours? late|showed up late",
    "quality": r"dirty|not clean|bad job|sloppy|poor|terrible|unhappy|not happy|disappoint|streak",
}


def classify_complaint(text: str) -> str | None:
    t = text.lower()
    for cat, pat in COMPLAINT.items():
        if re.search(pat, t):
            return cat
    if re.search(r"refund|money back|complain", t):
        return "quality"
    return None


def wants_booking(text: str) -> bool:
    return bool(re.search(r"\b(book|yes|yep|sure|let'?s do|sounds good|pay|checkout|check out|go ahead|schedule|reserve)\b", text.lower()))


def extract_contact(text: str) -> dict:
    out = {}
    if m := re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text):
        out["customer_email"] = m.group(0)
    if m := re.search(r"(?:my name is|i'?m|this is|name:)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)", text):
        out["customer_name"] = m.group(1)
    if m := re.search(r"\b(\d{1,5}\s+[A-Za-z0-9 .]+?(?:St|Street|Ave|Avenue|Rd|Road|Dr|Drive|Ln|Lane|Ct|Court|Blvd|Way|Pl)\b\.?)", text):
        out["address"] = m.group(1)
    if m := re.search(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|next week|\d{4}-\d{2}-\d{2}|(?:nov|dec|oct)\w*\.? \d{1,2})\b", text, re.I):
        out["service_date"] = m.group(1)
    return out
