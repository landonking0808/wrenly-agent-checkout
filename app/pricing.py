"""Turn structured home details into a price, from config/pricing.json. Pure functions, no AI."""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict

from . import config

FREQUENCIES = ("weekly", "biweekly", "monthly", "one_time")
FREQ_LABEL = {"weekly": "Weekly", "biweekly": "Every two weeks", "monthly": "Monthly", "one_time": "One-time"}


class QuoteError(ValueError):
    pass


@dataclass
class Quote:
    bedrooms: int
    bathrooms: float
    frequency: str
    add_ons: list[str]
    lines: list[dict] = field(default_factory=list)
    total: float = 0.0
    published_price: bool = False
    explanation: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def normalize_frequency(value: str | None) -> str:
    v = (value or "").lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "every_two_weeks": "biweekly", "bi_weekly": "biweekly", "fortnightly": "biweekly", "every_other_week": "biweekly",
        "once": "one_time", "onetime": "one_time", "single": "one_time", "one_off": "one_time", "once_a_month": "monthly",
        "every_week": "weekly", "every_month": "monthly",
    }
    v = aliases.get(v, v)
    if v not in FREQUENCIES:
        raise QuoteError(f"Unknown frequency '{value}'. Use one of {', '.join(FREQUENCIES)}.")
    return v


def build_quote(bedrooms: int, bathrooms: float | None = None, frequency: str = "biweekly", add_ons: list[str] | None = None) -> Quote:
    p = config.pricing()
    try:
        bedrooms = int(bedrooms)
    except (TypeError, ValueError):
        raise QuoteError("bedrooms must be a whole number")
    if bedrooms < 1:
        bedrooms = 1  # studio prices as a 1-bed
    if bedrooms > p["max_bedrooms"]:
        raise QuoteError(f"Homes over {p['max_bedrooms']} bedrooms need a walkthrough before we quote.")
    freq = normalize_frequency(frequency)
    included = float(p["included_bathrooms"][str(bedrooms)])
    baths = float(bathrooms) if bathrooms not in (None, "") else included
    if baths < 0 or baths > 8:
        raise QuoteError("bathrooms out of range")
    base = float(p["base_prices"][str(bedrooms)][freq])
    lines = [{"label": f"{bedrooms}-bedroom home, {FREQ_LABEL[freq].lower()} (includes {included:g} bath)", "amount": base}]
    extra_halves = max(0, math.ceil((baths - included) * 2 - 1e-9))
    if extra_halves:
        amt = extra_halves * float(p["extra_bathroom_price"]["per_half_bath"])
        lines.append({"label": f"Extra bathrooms (+{extra_halves * 0.5:g})", "amount": amt})
    cleaned_addons = []
    catalog = p["add_ons"]
    for a in add_ons or []:
        key = str(a).lower().strip().replace(" ", "_").replace("-", "_")
        if key not in catalog or key.startswith("_"):
            raise QuoteError(f"Unknown add-on '{a}'. Available: {', '.join(k for k in catalog if not k.startswith('_'))}")
        if key in cleaned_addons:
            continue
        cleaned_addons.append(key)
        lines.append({"label": catalog[key]["label"], "amount": float(catalog[key]["price"])})
    total = round(sum(l["amount"] for l in lines), 2)
    published = any(r["bedrooms"] == bedrooms and r["frequency"] == freq for r in p["published_prices"])
    q = Quote(bedrooms=bedrooms, bathrooms=baths, frequency=freq, add_ons=cleaned_addons, lines=lines, total=total, published_price=published)
    q.explanation = explain(q)
    return q


def explain(q: Quote) -> str:
    parts = [f"${l['amount']:.0f} {l['label']}" for l in q.lines]
    src = "Wrenly's published price" if q.published_price else "the sample price table (sandbox demo)"
    return f"{' + '.join(parts)} = ${q.total:.2f}. Base rate from {src}; regular visits cost less per visit than a one-time clean because the home stays maintained."
