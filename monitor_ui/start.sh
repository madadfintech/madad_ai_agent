#!/usr/bin/env bash
# Local launcher — backend + frontend in two background processes.
# Use from the repo root: ./monitor_ui/start.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"

# 1) Backend dependencies (one-time per machine).
if [[ ! -d "$ROOT/backend/.venv" ]]; then
    echo "Creating backend venv..."
    python3 -m venv "$ROOT/backend/.venv"
fi
"$ROOT/backend/.venv/bin/pip" install -q -r "$ROOT/backend/requirements.txt"

# 2) Frontend dependencies (one-time per machine).
if [[ ! -d "$ROOT/frontend/node_modules" ]]; then
    echo "Installing frontend deps..."
    (cd "$ROOT/frontend" && npm install --silent)
fi

echo
echo "=================================================="
echo "  Madad Monitor UI"
echo "  Backend:  http://127.0.0.1:5001"
echo "  Frontend: http://localhost:5173"
echo "=================================================="
echo

# Launch both, kill children on exit.
cleanup() {
    kill 0 2>/dev/null || true
}
trap cleanup EXIT

(cd "$ROOT/.." && "$ROOT/backend/.venv/bin/python" -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 5001) &
(cd "$ROOT/frontend" && npm run dev) &

wait
