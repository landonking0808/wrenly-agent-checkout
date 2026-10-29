import pytest

from app import extract, pricing


@pytest.mark.parametrize("text,expect", [
    ("3 bed, 2 bath, every two weeks", {"bedrooms": 3, "bathrooms": 2, "frequency": "biweekly"}),
    ("We have a three-bedroom house with 2.5 baths, biweekly please", {"bedrooms": 3, "bathrooms": 2.5, "frequency": "biweekly"}),
    ("2 bedroom apartment, monthly, we have a dog", {"bedrooms": 2, "frequency": "monthly", "add_ons": ["pets"]}),
    ("4 bed 3 bath one-time move-out clean with oven and fridge", {"bedrooms": 4, "bathrooms": 3, "frequency": "one_time"}),
    ("studio, weekly", {"bedrooms": 1, "frequency": "weekly"}),
    ("two bedrooms and two and a half bathrooms every other week", {"bedrooms": 2, "bathrooms": 2.5, "frequency": "biweekly"}),
])
def test_extract_home(text, expect):
    got = extract.extract_home(text)
    for k, v in expect.items():
        assert got.get(k) == v, (k, got)


def test_extract_addons():
    got = extract.extract_home("4 bed one time, inside the oven and fridge, plus interior windows")
    assert set(got["add_ons"]) == {"inside_oven", "inside_fridge", "interior_windows"}


def test_published_price_3bed_biweekly_is_179():
    q = pricing.build_quote(3, 2, "biweekly")
    assert q.total == 179
    assert q.published_price is True
    assert "published price" in q.explanation


def test_sample_prices_labeled():
    q = pricing.build_quote(2, None, "monthly")
    assert q.published_price is False
    assert "sample" in q.explanation


def test_extra_baths_and_addons():
    q = pricing.build_quote(3, 3, "biweekly", ["pets", "inside_oven"])
    # 179 base + 2 extra half-baths x $12 + 15 + 35
    assert q.total == 179 + 24 + 15 + 35
    assert [l["label"] for l in q.lines][1].startswith("Extra bathrooms")


def test_frequency_aliases():
    assert pricing.normalize_frequency("every two weeks") == "biweekly"
    assert pricing.normalize_frequency("once") == "one_time"
    with pytest.raises(pricing.QuoteError):
        pricing.normalize_frequency("hourly")


def test_too_big_needs_walkthrough():
    with pytest.raises(pricing.QuoteError):
        pricing.build_quote(9, 4, "weekly")


def test_unknown_addon_rejected():
    with pytest.raises(pricing.QuoteError):
        pricing.build_quote(2, 1, "weekly", ["gold_plating"])


def test_complaint_classifier():
    assert extract.classify_complaint("the crew missed the upstairs bathroom") == "missed_area"
    assert extract.classify_complaint("nobody came today") == "no_show"
    assert extract.classify_complaint("they broke a lamp") == "damage"
    assert extract.classify_complaint("the floors are still dirty") == "quality"
    assert extract.classify_complaint("thanks, looks great") is None


def test_chat_offline_quotes_and_asks_frequency(api):
    r = api.post("/api/chat", json={"message": "I have a 3 bedroom house"}).json()
    assert "How often" in r["reply"]
    r = api.post("/api/chat", json={"message": "every two weeks", "chat_id": r["chat_id"]}).json()
    q = next(e for e in r["events"] if e["type"] == "quote")["quote"]
    assert q["total"] == 179 and "$179.00" in r["reply"]
    assert r["engine"].startswith("offline")
