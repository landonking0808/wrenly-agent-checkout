from app import margin


def test_179_biweekly_passes():
    m = margin.check(179, 179)
    assert m["crew_pay"] == 125.30
    assert m["paypal_fee_est"] == round(179 * 0.0349 + 0.49, 2)
    assert m["floor"] == 17.90  # 10% beats $15
    assert m["margin"] == round(179 - 125.30 - m["paypal_fee_est"] - 0.25, 2)
    assert m["ok"]


def test_floor_is_15_for_small_jobs():
    m = margin.check(109, 109)
    assert m["floor"] == 15.0
    assert m["ok"]  # 109 - 76.30 - 4.29 - .25 = 28.16


def test_extra_hours_can_fail_margin():
    m = margin.check(179, 179, extra_hours=1.5)
    assert m["crew_pay"] == 125.30 + 42
    assert not m["ok"]
    assert "below the floor" in m["reasons"][0]


def test_refund_reduces_margin():
    m = margin.check(179, 179, refunded=30)
    assert not m["ok"]


def test_nothing_captured_fails():
    m = margin.check(179, 0)
    assert not m["ok"] and "nothing captured" in m["reasons"][0]


def test_payout_cap():
    m = margin.check(800, 800)
    assert any("cap" in r for r in m["reasons"])
