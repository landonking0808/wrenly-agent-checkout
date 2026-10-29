"""SQLite store. The ledger table is append-only: triggers reject UPDATE and DELETE, and every row is mirrored to a JSONL file."""
from __future__ import annotations

import json
import sqlite3
import threading

import uuid
from datetime import datetime, timezone

from . import config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_conn_path = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  customer_name TEXT, customer_email TEXT, address TEXT, service_date TEXT,
  bedrooms INTEGER, bathrooms REAL, frequency TEXT, add_ons TEXT,
  quote_json TEXT, list_price REAL NOT NULL,
  crew_name TEXT, crew_email TEXT,
  status TEXT NOT NULL,
  order_id TEXT, authorization_id TEXT, capture_id TEXT, payout_batch_id TEXT, payout_item_id TEXT,
  captured_amount REAL DEFAULT 0, refunded_amount REAL DEFAULT 0, payout_amount REAL DEFAULT 0,
  checklist_json TEXT, extra_hours REAL DEFAULT 0, margin_json TEXT, notes TEXT
);
CREATE TABLE IF NOT EXISTS ledger (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  job_id TEXT,
  action TEXT NOT NULL,
  status TEXT NOT NULL,
  amount REAL,
  currency TEXT DEFAULT 'USD',
  paypal_id TEXT,
  request_id TEXT,
  actor TEXT NOT NULL,
  detail TEXT
);
CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  job_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  amount REAL,
  reason TEXT,
  proposal_json TEXT,
  status TEXT NOT NULL,
  decided_at TEXT, decided_by TEXT, result_json TEXT
);
CREATE TABLE IF NOT EXISTS chats (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  history_json TEXT NOT NULL,
  state_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS webhook_events (
  id TEXT PRIMARY KEY,
  received_at TEXT NOT NULL,
  event_type TEXT, resource_id TEXT, verified TEXT, body TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def conn() -> sqlite3.Connection:
    global _conn, _conn_path
    path = config.db_path()
    with _lock:
        if _conn is None or _conn_path != path:
            path.parent.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.executescript(SCHEMA)
            _conn_path = path
        return _conn


def reset_for_tests() -> None:
    global _conn, _conn_path
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None
        _conn_path = None


def q(sql: str, args: tuple = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]


def q1(sql: str, args: tuple = ()) -> dict | None:
    rows = q(sql, args)
    return rows[0] if rows else None


def ex(sql: str, args: tuple = ()) -> None:
    with _lock:
        conn().execute(sql, args)


# ---- ledger -------------------------------------------------------------

def ledger_append(action: str, status: str, *, job_id: str | None = None, amount: float | None = None,
                  paypal_id: str | None = None, request_id: str | None = None, actor: str = "agent",
                  detail: dict | None = None) -> dict:
    row = {"ts": now_iso(), "job_id": job_id, "action": action, "status": status, "amount": amount, "currency": "USD",
           "paypal_id": paypal_id, "request_id": request_id, "actor": actor, "detail": json.dumps(detail or {}, default=str)}
    with _lock:
        cur = conn().execute(
            "INSERT INTO ledger (ts, job_id, action, status, amount, currency, paypal_id, request_id, actor, detail) VALUES (?,?,?,?,?,?,?,?,?,?)",
            tuple(row.values()))
        row["seq"] = cur.lastrowid
        p = config.ledger_jsonl_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps(row) + "\n")
    return row


def ledger_find_ok(request_id: str) -> dict | None:
    """The idempotency check: a money action that already succeeded under this request id."""
    return q1("SELECT * FROM ledger WHERE request_id=? AND status='ok' ORDER BY seq DESC LIMIT 1", (request_id,))


def ledger_rows(job_id: str | None = None, limit: int = 500) -> list[dict]:
    if job_id:
        rows = q("SELECT * FROM ledger WHERE job_id=? ORDER BY seq DESC LIMIT ?", (job_id, limit))
    else:
        rows = q("SELECT * FROM ledger ORDER BY seq DESC LIMIT ?", (limit,))
    for r in rows:
        r["detail"] = json.loads(r["detail"] or "{}")
    return rows


# ---- jobs ---------------------------------------------------------------

def job(job_id: str) -> dict | None:
    j = q1("SELECT * FROM jobs WHERE id=?", (job_id,))
    if j:
        for k in ("quote_json", "checklist_json", "margin_json"):
            j[k[:-5]] = json.loads(j[k]) if j.get(k) else None
        j["add_ons"] = json.loads(j["add_ons"] or "[]")
    return j


def jobs() -> list[dict]:
    return [job(r["id"]) for r in q("SELECT id FROM jobs ORDER BY created_at DESC, rowid DESC")]


def job_update(job_id: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    ex(f"UPDATE jobs SET {cols} WHERE id=?", tuple(fields.values()) + (job_id,))


def job_create(**fields) -> str:
    jid = fields.pop("id", None) or new_id("job")
    fields.setdefault("created_at", now_iso())
    fields.setdefault("status", "quoted")
    cols = ", ".join(["id"] + list(fields))
    ex(f"INSERT INTO jobs ({cols}) VALUES ({', '.join('?' * (len(fields) + 1))})", (jid, *fields.values()))
    return jid


def approvals(status: str | None = None) -> list[dict]:
    rows = q("SELECT * FROM approvals " + ("WHERE status=? " if status else "") + "ORDER BY created_at DESC, rowid DESC", (status,) if status else ())
    for r in rows:
        r["proposal"] = json.loads(r.pop("proposal_json") or "{}")
        r["result"] = json.loads(r.pop("result_json") or "null")
    return rows


def approval(aid: str) -> dict | None:
    for a in q("SELECT id FROM approvals WHERE id=?", (aid,)):
        return next(x for x in approvals() if x["id"] == a["id"])
    return None


