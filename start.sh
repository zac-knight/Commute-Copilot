#!/usr/bin/env bash
# One command to run the whole app: API (:8000) + web UI (:5173).
#   ./start.sh          real assistant (needs Ollama running: `ollama serve`)
#   ./start.sh demo     demo mode: recorded answers, fake calendar, no model needed
set -euo pipefail
cd "$(dirname "$0")"

MODE="${1:-live}"
[ -d frontend/node_modules ] || (cd frontend && npm install)
[ -d backend/.venv ] || (cd backend && uv sync)

if [ "$MODE" = "demo" ]; then
  export DEMO=1
elif ! curl -s -m 2 localhost:11434/api/version >/dev/null; then
  echo "Ollama is not running. Start it (open the app or run 'ollama serve'), or use: ./start.sh demo"
  exit 1
fi

(cd backend && uv run uvicorn app.main:app --port 8000 --reload --reload-dir app) &
API=$!
(cd frontend && npm run dev -- --open) &
UI=$!
trap 'kill $API $UI 2>/dev/null' EXIT INT TERM

echo "UI: http://localhost:5173   API: http://localhost:8000   (Ctrl-C to stop)"
wait
