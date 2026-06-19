#!/usr/bin/env bash
# Stop the Madad Monitor UI daemons.
#
# Usage:  ./monitor_ui/stop.sh

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$ROOT/.run"

stop_daemon() {
    local name="$1"
    local pidfile="$RUN_DIR/$name.pid"
    if [[ ! -f "$pidfile" ]]; then
        echo "$name : not running"
        return
    fi
    local pid
    pid="$(cat "$pidfile" 2>/dev/null || true)"
    if [[ -z "$pid" ]]; then
        rm -f "$pidfile"
        echo "$name : pid file empty, removed"
        return
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
        rm -f "$pidfile"
        echo "$name : already gone (pid $pid)"
        return
    fi
    # Kill the whole process group. setsid in start.sh makes pgid==pid;
    # negative kill target = process group.
    kill -TERM "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    # Give it a moment, then SIGKILL if still alive.
    for _ in 1 2 3 4 5; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.2
    done
    if kill -0 "$pid" 2>/dev/null; then
        kill -KILL "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    fi
    rm -f "$pidfile"
    echo "$name : stopped (pid $pid)"
}

if [[ ! -d "$RUN_DIR" ]]; then
    echo "Nothing to stop - .run/ does not exist."
    exit 0
fi

stop_daemon frontend
stop_daemon backend
