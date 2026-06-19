#!/usr/bin/env bash
# Quick status check for the Madad Monitor UI daemons.
#
# Usage:  ./monitor_ui/status.sh

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$ROOT/.run"

print_row() {
    local name="$1" port="$2"
    local pidfile="$RUN_DIR/$name.pid"
    if [[ ! -f "$pidfile" ]]; then
        printf "  %-9s stopped    pid=-     port=%s\n" "$name" "$port"
        return
    fi
    local pid
    pid="$(cat "$pidfile" 2>/dev/null || true)"
    if [[ -z "$pid" ]]; then
        printf "  %-9s stale      pid=-     port=%s\n" "$name" "$port"
        return
    fi
    if kill -0 "$pid" 2>/dev/null; then
        printf "  %-9s running    pid=%-5s port=%s\n" "$name" "$pid" "$port"
    else
        printf "  %-9s stale      pid=%-5s port=%s\n" "$name" "$pid" "$port"
    fi
}

echo
echo "Madad Monitor UI"
echo "-----------------"
print_row backend 5001
print_row frontend 5173
echo

# Probe backend /api/connection for tunnel + monitor health.
if [[ -f "$RUN_DIR/backend.pid" ]] && kill -0 "$(cat "$RUN_DIR/backend.pid")" 2>/dev/null; then
    if command -v curl >/dev/null 2>&1; then
        body="$(curl -fsS --max-time 3 http://127.0.0.1:5001/api/connection 2>/dev/null || true)"
        if [[ -n "$body" ]]; then
            tunnel_open="$(echo "$body" | grep -oE '"open"[ ]*:[ ]*(true|false)' | head -n1 | grep -oE '(true|false)' || echo unknown)"
            monitor_ok="$(echo "$body" | grep -oE '"ok"[ ]*:[ ]*(true|false)' | head -n1 | grep -oE '(true|false)' || echo unknown)"
            echo "  tunnel    : $tunnel_open"
            echo "  monitor   : $monitor_ok"
            echo
        else
            echo "  (backend not yet responsive - give it ~2s after start)"
            echo
        fi
    fi
fi
