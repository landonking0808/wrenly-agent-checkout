#!/usr/bin/env bash
# One command: make a virtualenv, install, and start Wrenly Agent Checkout on http://localhost:8000
set -euo pipefail
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
if [ ! -x .venv/bin/python ]; then "$PY" -m venv .venv; fi
.venv/bin/pip install -q -r requirements.txt
[ -f .env ] || { cp .env.example .env; echo "Created .env from .env.example (mock PayPal + offline AI until you add keys)."; }
PORT=${PORT:-8000}
echo "Booking page:   http://localhost:$PORT/"
echo "Ops dashboard:  http://localhost:$PORT/ops"
exec .venv/bin/uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "$PORT"
