"""The margin rule: crew gets 70% of list; the platform must keep at least max($15, 10%) after PayPal fees."""
from __future__ import annotations

from . import config


def r2(x: float) -> float:
    return round(float(x) + 1e-9, 2)


def paypal_fee(amount: float) -> float:
    pol = config.policy()
    return r2(amount * pol["paypal_fee_pct"] + pol["paypal_fee_fixed"]) if amount > 0 else 0.0


def crew_pay(list_price: float, extra_hours: float = 0.0) -> float:
    pol = config.policy()
    return r2(list_price * pol["crew_share_pct"] + max(0.0, extra_hours) * pol["crew_extra_hour_rate"])


def check(list_price: float, captured: float, refunded: float = 0.0, extra_hours: float = 0.0) -> dict:
    """Return the margin breakdown and whether a crew payout may go out automatically."""
    pol = config.policy()
    net_revenue = r2(captured - refunded)
    crew = crew_pay(list_price, extra_hours)
    fee_in = paypal_fee(captured)
    fee_out = pol["payout_fee_fixed"]
    margin = r2(net_revenue - crew - fee_in - fee_out)
    floor = r2(max(pol["margin_floor_abs"], pol["margin_floor_pct"] * list_price))
    reasons = []
    if captured <= 0:
        reasons.append("nothing captured yet")
    if margin < floor:
        reasons.append(f"margin ${margin:.2f} is below the floor ${floor:.2f}")
    if crew > pol["max_payout_amount"]:
        reasons.append(f"crew pay ${crew:.2f} is over the auto-payout cap ${pol['max_payout_amount']:.2f}")
    return {
        "list_price": r2(list_price), "captured": r2(captured), "refunded": r2(refunded), "net_revenue": net_revenue,
        "crew_pay": crew, "extra_hours": extra_hours, "paypal_fee_est": fee_in, "payout_fee_est": fee_out,
        "margin": margin, "margin_pct": r2(100 * margin / list_price) if list_price else 0.0, "floor": floor,
        "ok": not reasons, "reasons": reasons,
    }
