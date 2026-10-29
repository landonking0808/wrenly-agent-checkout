"""Settings and config files. Everything comes from env vars or config/*.json; nothing secret is hard-coded."""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def mock_paypal() -> bool:
    if env("MOCK_PAYPAL"):
        return env("MOCK_PAYPAL") not in ("0", "false", "no")
    # No credentials -> mock, so the app always runs.
    return not (env("PAYPAL_CLIENT_ID") and env("PAYPAL_CLIENT_SECRET"))


def paypal_base() -> str:
    return "https://api-m.paypal.com" if env("PAYPAL_ENV", "sandbox") == "live" else "https://api-m.sandbox.paypal.com"


def db_path() -> Path:
    return Path(env("WRENLY_DB", str(ROOT / "data" / "wrenly.db")))


def ledger_jsonl_path() -> Path:
    return Path(env("WRENLY_LEDGER_JSONL", str(db_path().with_name("ledger.jsonl"))))


@lru_cache
def pricing() -> dict:
    return json.loads((ROOT / "config" / "pricing.json").read_text())


@lru_cache
def policy() -> dict:
    return json.loads((ROOT / "config" / "policy.json").read_text())


def gemini_key() -> str:
    return env("GEMINI_API_KEY")


def gemini_models() -> list[str]:
    raw = env("GEMINI_MODEL", "gemini-3.5-flash,gemini-2.5-flash,gemini-3.1-flash-lite")
    return [m.strip() for m in raw.split(",") if m.strip()]


def ai_enabled() -> bool:
    return bool(gemini_key()) and env("WRENLY_OFFLINE_AI", "0") in ("0", "", "false")
