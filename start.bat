@echo off
rem One-command start on Windows: creates .venv, installs deps, runs Margin at http://localhost:8000
cd /d "%~dp0"
if not exist .venv python -m venv .venv
.venv\Scripts\pip install -q -r requirements.txt
if not exist .env copy .env.example .env
echo Margin running at http://localhost:8000
.venv\Scripts\uvicorn server.app:app --host 0.0.0.0 --port 8000
