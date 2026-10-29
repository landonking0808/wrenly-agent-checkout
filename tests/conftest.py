import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["MOCK_PAYPAL"] = "1"
os.environ["WRENLY_OFFLINE_AI"] = "1"


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("WRENLY_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("WRENLY_LEDGER_JSONL", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("MOCK_PAYPAL", "1")
    monkeypatch.setenv("WRENLY_OFFLINE_AI", "1")
    from app import db, paypal
    db.reset_for_tests()
    pp = paypal.MockPayPal()
    paypal.set_client(pp)
    yield pp
    db.reset_for_tests()


@pytest.fixture
def api():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def book(api, text="3 bed 2 bath every two weeks", chat_id=None):
    """Quote + checkout + mock approval + authorize through the HTTP API. Returns (chat_id, job_id)."""
    r = api.post("/api/chat", json={"message": text, "chat_id": chat_id}).json()
    cid = r["chat_id"]
    r = api.post("/api/chat", json={"message": "yes, book it", "chat_id": cid}).json()
    ev = next(e for e in r["events"] if e["type"] == "checkout")
    assert api.post(f"/api/mock/approve/{ev['order_id']}").status_code == 200
    a = api.post(f"/api/orders/{ev['order_id']}/authorize", json={"job_id": ev["job_id"]})
    assert a.status_code == 200, a.text
    return cid, ev["job_id"]


def full_checklist():
    from app import config
    return {c: True for c in config.policy()["checklist"]}
