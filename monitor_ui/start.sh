#!/usr/bin/env bash
# Start the Madad Monitor UI (backend + frontend) as detached background
# processes. Returns control to your shell immediately; use stop.sh to
# stop. Logs are written under monitor_ui/.run/*.log.
#
# Usage:  ./monitor_ui/start.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$ROOT/.run"
mkdir -p "$RUN_DIR"

is_running() {
    local name="$1"
    local pidfile="$RUN_DIR/$name.pid"
    [[ -f "$pidfile" ]] || return 1
    local pid
    pid="$(cat "$pidfile" 2>/dev/null || true)"
    [[ -n "$pid" ]] || return 1
    kill -0 "$pid" 2>/dev/null
}

if is_running backend || is_running frontend; then
    echo "Madad Monitor UI is already running."
    echo "Run ./monitor_ui/stop.sh first, or ./monitor_ui/status.sh to check."
    exit 1
fi

# 1) Backend deps (one-time per machine).
if [[ ! -d "$ROOT/backend/.venv" ]]; then
    echo "Creating backend venv..."
    python3 -m venv "$ROOT/backend/.venv"
fi
echo "Installing backend deps..."
"$ROOT/backend/.venv/bin/pip" install -q -r "$ROOT/backend/requirements.txt"

# 2) Frontend deps (one-time per machine).
if [[ ! -d "$ROOT/frontend/node_modules" ]]; then
    echo "Installing frontend deps..."
    (cd "$ROOT/frontend" && npm install --silent --no-audit --no-fund)
fi

# 3) Launch detached. setsid puts each daemon in its own process group
# so stop.sh can kill the whole tree (npm spawns node/vite as a child;
# uvicorn may spawn reloader subprocesses).
BACKEND_LOG="$RUN_DIR/backend.log"
FRONTEND_LOG="$RUN_DIR/frontend.log"

start_detached() {
    local name="$1"; shift
    local workdir="$1"; shift
    local logfile="$1"; shift
    if command -v setsid >/dev/null 2>&1; then
        ( cd "$workdir" && setsid nohup "$@" >"$logfile" 2>&1 < /dev/null & echo $! > "$RUN_DIR/$name.pid" )
    else
        ( cd "$workdir" && nohup "$@" >"$logfile" 2>&1 < /dev/null & echo $! > "$RUN_DIR/$name.pid" )
    fi
}

start_detached backend "$ROOT" "$BACKEND_LOG" \
    "$ROOT/backend/.venv/bin/python" -m uvicorn backend.main:app \
    --host 127.0.0.1 --port 5001

start_detached frontend "$ROOT/frontend" "$FRONTEND_LOG" \
    npm run dev

sleep 2

BACKEND_PID="$(cat "$RUN_DIR/backend.pid")"
FRONTEND_PID="$(cat "$RUN_DIR/frontend.pid")"

cat <<EOF

==================================================
  Madad Monitor UI - started
  Backend  : http://127.0.0.1:5001  (PID $BACKEND_PID)
  Frontend : http://localhost:5173  (PID $FRONTEND_PID)
  Logs     : $RUN_DIR
==================================================

Tail logs:
  tail -f $BACKEND_LOG
  tail -f $FRONTEND_LOG

Stop:    ./monitor_ui/stop.sh
Status:  ./monitor_ui/status.sh
EOF
