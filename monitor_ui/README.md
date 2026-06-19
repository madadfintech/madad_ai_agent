# Madad Monitor UI

A local React frontend (and tiny FastAPI backend) to manage the staging
log monitor — view captured issues, stream live events, clear the feed,
wipe test users, and keep a local history that survives `/monitor/clear`.

Local-only by design. No deployment, no auth surface beyond the
existing admin bearer token. Runs entirely on your machine; opens an
SSH tunnel to the staging VM on startup.

## Tech stack

- **Backend** — Python 3.13 + FastAPI + sshtunnel + SQLite via SQLModel
- **Frontend** — Vite + React 18 + TypeScript + Tailwind + TanStack Query
- **Storage** — SQLite at `monitor_ui/history.db` (events, saved
  identities, cleanup audit log)

## First-time setup

```bash
# 1. Copy your admin token from the staging .env
cp monitor_ui/.env.example monitor_ui/.env
# Then edit monitor_ui/.env and paste ADMIN_API_TOKEN

# 2. Make sure these are on your machine:
#    - Python 3.11+
#    - Node.js 20+
#    - SSH key at staging_server_access/jathish_madad_agent_staging
```

## Run it

**Windows:**

```powershell
.\monitor_ui\start.ps1
```

**macOS / Linux:**

```bash
./monitor_ui/start.sh
```

Both scripts:

1. Create a Python venv at `monitor_ui/backend/.venv` (one-time)
2. Install backend deps (one-time)
3. Install frontend deps via `npm install` (one-time)
4. Launch the backend on `http://127.0.0.1:5001`
5. Launch the Vite dev server on `http://localhost:5173`

Open <http://localhost:5173> in your browser.

## Pages

| Page | What |
|---|---|
| **Dashboard** | Live stats (total / by severity), grouped breakdowns by rule + container, a real-time ticker (SSE streamed + 3s polling fallback), one-click monitor clear. |
| **Issues** | Full filterable table (live vs local SQLite history), filters by severity / rule / container, manual reload. |
| **Test Users** | Saved identities (SQLite-backed) with one-click selection, ad-hoc identity input, SQL LIKE pattern wipe, **dry-run** preview, audit log of every cleanup. |
| **Settings** | SSH tunnel status + reopen, local history clear, full list of loaded regex + behavioral rules from the monitor. |

## Architecture

```
┌─────────────────┐   HTTP /api/*    ┌──────────────────┐
│ React (Vite)    │ ───────────────▶ │ FastAPI backend  │
│ localhost:5173  │                  │ localhost:5001   │
└─────────────────┘                  │                  │
                                     │  ┌────────────┐  │
                                     │  │ SQLite     │  │
                                     │  │ history.db │  │
                                     │  └────────────┘  │
                                     │                  │
                                     │  SSH tunnel ↓    │
                                     └────────┬─────────┘
                                              │ 127.0.0.1:NNN
                                              ▼
                              ┌─────────────────────────────┐
                              │ Staging VM (jathish@34.18..)│
                              │ log_monitor :8090           │
                              │ docker compose exec workflow│
                              └─────────────────────────────┘
```

The backend keeps a single SSH tunnel open via `sshtunnel`/paramiko.
Every monitor API call (`/monitor/stats`, `/monitor/tail`, etc.) is
proxied through `httpx` over the tunnel. The SSE stream
(`/monitor/stream`) is pass-through.

A background poller pulls `/monitor/tail` every 5s and inserts any new
events into the local SQLite. That's how the "History" toggle on the
Issues page can show events from before a `/monitor/clear`.

The Test Users page builds the cleanup CLI args, then runs
`docker compose exec workflow python -m scripts.cleanup_test_users …`
on the VM over SSH. So the UI reuses the same script we already trust;
no parallel cleanup paths.

## Layout

```
monitor_ui/
├── README.md
├── start.sh / start.ps1     # one-command launchers
├── .env.example
├── backend/
│   ├── main.py              # FastAPI app + endpoints
│   ├── tunnel.py            # SSH tunnel manager
│   ├── db.py                # SQLite + models
│   ├── config.py            # env-driven config
│   └── requirements.txt
└── frontend/
    ├── package.json
    ├── vite.config.ts
    ├── tailwind.config.js
    ├── index.html
    └── src/
        ├── main.tsx
        ├── App.tsx
        ├── lib/api.ts       # typed API client
        ├── components/Layout.tsx
        └── pages/{Dashboard,Issues,TestUsers,Settings}.tsx
```

## Configuration

The backend reads:

1. `monitor_ui/.env` (this dir — first preference)
2. The repo-root `.env` (fallback)
3. Process environment

Required:

```
ADMIN_API_TOKEN=<bearer token from staging>
```

All other settings have sensible defaults (see `backend/config.py`).
Override any of them in `monitor_ui/.env` if your staging host or SSH
key path differs from the team default.

## Stopping it

`Ctrl-C` in the terminal where you launched it. Both processes exit
together (`trap cleanup EXIT` in `start.sh`; jobs are killed in
`start.ps1`).
