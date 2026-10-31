#!/usr/bin/env bash
# One-command start: creates .venv, installs deps, runs Margin at http://localhost:8000
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
[ -f .env ] || cp .env.example .env
echo "Margin running at http://localhost:8000"
exec .venv/bin/uvicorn server.app:app --port 8000
