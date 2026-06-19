"""Local monitor-UI backend configuration.

Reads .env at the repo root (one directory up from monitor_ui/) plus
monitor_ui/.env so the user can pick whichever they prefer. SSH key
defaults to the existing staging key already in the repo.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_REPO_ROOT / ".env", override=False)
load_dotenv(_REPO_ROOT / "monitor_ui" / ".env", override=True)


SSH_HOST = os.environ.get("MONITOR_SSH_HOST", "34.18.50.97")
SSH_USER = os.environ.get("MONITOR_SSH_USER", "jathish")
SSH_PORT = int(os.environ.get("MONITOR_SSH_PORT", "22"))
SSH_KEY_PATH = os.environ.get(
    "MONITOR_SSH_KEY_PATH",
    str(_REPO_ROOT / "staging_server_access" / "jathish_madad_agent_staging"),
)

# Remote bind — log_monitor binds to 127.0.0.1:8090 inside the VM.
REMOTE_BIND = (
    os.environ.get("MONITOR_REMOTE_HOST", "127.0.0.1"),
    int(os.environ.get("MONITOR_REMOTE_PORT", "8090")),
)
# Local bind — random high port chosen automatically by sshtunnel so
# nothing on the user's box clashes with the remote 8090.
LOCAL_BIND_PORT = int(os.environ.get("MONITOR_LOCAL_BIND_PORT", "0"))  # 0 = auto

# Bearer token for the log_monitor admin API. Required.
ADMIN_API_TOKEN = os.environ.get("ADMIN_API_TOKEN", "")

# SQLite database path — keeps history across monitor /clear calls.
SQLITE_PATH = os.environ.get(
    "MONITOR_UI_DB",
    str(_REPO_ROOT / "monitor_ui" / "history.db"),
)

# CORS — Vite default + a few common dev hosts.
ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://localhost:5174",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:5174",
]
