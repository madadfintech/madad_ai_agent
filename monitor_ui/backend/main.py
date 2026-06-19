"""Local monitor-UI backend.

A tiny FastAPI app that:
* Opens an SSH tunnel to the staging log_monitor (auto, on startup).
* Proxies the monitor admin API to the local React frontend.
* Persists every captured event in SQLite so history survives
  /monitor/clear on the remote side.
* Exposes saved test-user identities + an audit log of cleanups.

Runs locally on http://127.0.0.1:5001 — talk to it from the Vite
frontend at http://localhost:5173 (CORS allowed).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import subprocess
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import select

from .config import (
    ADMIN_API_TOKEN,
    ALLOWED_ORIGINS,
    SSH_HOST,
    SSH_KEY_PATH,
    SSH_USER,
)
from .db import Cleanup, Event, Identity, event_key, get_session, init_db
from .tunnel import TunnelState

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("monitor_ui")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    init_db()
    TunnelState.get().open()
    poll_task = asyncio.create_task(_poll_history_loop())
    try:
        yield
    finally:
        poll_task.cancel()
        TunnelState.get().close()


app = FastAPI(title="Madad Monitor UI", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Helpers — talk to the remote monitor through the tunnel
# ---------------------------------------------------------------------------


def _monitor_url(path: str) -> str:
    base = TunnelState.get().base_url
    if base is None:
        raise HTTPException(
            503, detail="SSH tunnel not open — check /api/connection"
        )
    return f"{base}{path}"


async def _monitor_get(path: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            _monitor_url(path),
            headers={"Authorization": f"Bearer {ADMIN_API_TOKEN}"},
        )
        r.raise_for_status()
        return r.json()


async def _monitor_post(path: str, body: dict | None = None) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            _monitor_url(path),
            headers={"Authorization": f"Bearer {ADMIN_API_TOKEN}"},
            json=body or {},
        )
        r.raise_for_status()
        return r.json() if r.content else {}


# ---------------------------------------------------------------------------
# Connection / health
# ---------------------------------------------------------------------------


@app.get("/api/connection")
async def connection() -> dict[str, Any]:
    """Tunnel + monitor health in one shot. Frontend polls this on the
    Settings page to show ✅/⚠️."""
    state = TunnelState.get().status
    if state["open"]:
        try:
            monitor_health = await _monitor_get("/monitor/health")
            return {"tunnel": state, "monitor": monitor_health, "ok": True}
        except Exception as exc:  # noqa: BLE001
            return {
                "tunnel": state,
                "monitor": {"error": str(exc)[:200]},
                "ok": False,
            }
    return {"tunnel": state, "monitor": None, "ok": False}


@app.post("/api/connection/reopen")
async def reopen_tunnel() -> dict[str, Any]:
    TunnelState.get().close()
    TunnelState.get().open()
    return await connection()


# ---------------------------------------------------------------------------
# Issues — proxied from monitor + persisted to SQLite
# ---------------------------------------------------------------------------


_LINE_RE = re.compile(
    r"^(?P<at>\S+)\s*\|\s*(?P<severity>\S+)\s*\|\s*(?P<container>\S+)\s*\|\s*"
    r"(?P<rule>\S+)\s*\|\s*(?P<line>.+)$"
)


def _parse_issue_line(text: str) -> dict[str, str] | None:
    m = _LINE_RE.match(text)
    return m.groupdict() if m else None


async def _poll_history_loop() -> None:
    """Every 5s, pull the monitor's tail + insert any new events into
    the local SQLite. Keeps history after a remote /clear."""
    while True:
        try:
            await asyncio.sleep(5)
            if TunnelState.get().base_url is None:
                continue
            data = await _monitor_get("/monitor/tail?n=200")
            with get_session() as s:
                for raw in (data.get("lines") or []):
                    parsed = _parse_issue_line(raw)
                    if parsed is None:
                        continue
                    key = event_key(
                        parsed["at"], parsed["container"],
                        parsed["rule"], parsed["line"],
                    )
                    exists = s.exec(
                        select(Event.id).where(Event.key == key)
                    ).first()
                    if exists is not None:
                        continue
                    s.add(Event(
                        at=parsed["at"],
                        container=parsed["container"],
                        rule=parsed["rule"],
                        severity=parsed["severity"],
                        description="",
                        line=parsed["line"],
                        key=key,
                    ))
                s.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — keep the loop alive
            log.warning("poll loop error: %s", exc)


@app.get("/api/stats")
async def stats_proxy() -> dict[str, Any]:
    return await _monitor_get("/monitor/stats")


@app.get("/api/rules")
async def rules_proxy() -> dict[str, Any]:
    return await _monitor_get("/monitor/rules")


@app.get("/api/issues")
async def list_issues(
    n: int = 200,
    severity: str | None = None,
    rule: str | None = None,
    container: str | None = None,
    source: str = "live",  # "live" -> proxy; "history" -> SQLite
) -> dict[str, Any]:
    """List recent issues. ``source=live`` queries the running monitor;
    ``source=history`` reads from the local SQLite snapshot (survives
    monitor /clear)."""
    if source == "live":
        data = await _monitor_get(f"/monitor/tail?n={n}")
        rows: list[dict[str, Any]] = []
        for raw in data.get("lines") or []:
            parsed = _parse_issue_line(raw)
            if parsed is None:
                continue
            if severity and parsed["severity"] != severity:
                continue
            if rule and parsed["rule"] != rule:
                continue
            if container and parsed["container"] != container:
                continue
            rows.append(parsed)
        return {"issues": rows, "count": len(rows), "source": "live"}

    # source == "history"
    with get_session() as s:
        q = select(Event).order_by(Event.id.desc()).limit(n)  # type: ignore[arg-type]
        # Simple python-side filters — small N keeps this cheap.
        rows = []
        for ev in s.exec(q).all():
            if severity and ev.severity != severity:
                continue
            if rule and ev.rule != rule:
                continue
            if container and ev.container != container:
                continue
            rows.append({
                "at": ev.at,
                "severity": ev.severity,
                "container": ev.container,
                "rule": ev.rule,
                "line": ev.line,
            })
        return {"issues": rows, "count": len(rows), "source": "history"}


@app.post("/api/clear")
async def clear_monitor() -> dict[str, Any]:
    """Clear ONLY the remote monitor's issues file. The local SQLite
    history is preserved deliberately — that's the whole point of it.
    Use /api/history/clear if you also want a fresh history."""
    return await _monitor_post("/monitor/clear")


@app.post("/api/history/clear")
async def clear_history() -> dict[str, Any]:
    with get_session() as s:
        s.exec(Event.__table__.delete())  # type: ignore[attr-defined]
        s.commit()
    return {"cleared_history": True}


# ---------------------------------------------------------------------------
# SSE stream — proxy the monitor's live stream through
# ---------------------------------------------------------------------------


@app.get("/api/stream")
async def stream() -> StreamingResponse:
    async def gen() -> AsyncIterator[bytes]:
        url = _monitor_url("/monitor/stream")
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "GET", url,
                headers={"Authorization": f"Bearer {ADMIN_API_TOKEN}"},
            ) as r:
                async for chunk in r.aiter_bytes():
                    yield chunk

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Saved identities + wipe
# ---------------------------------------------------------------------------


class IdentityIn(BaseModel):
    identity: str
    label: str = ""


@app.get("/api/identities")
async def list_identities() -> dict[str, Any]:
    with get_session() as s:
        rows = s.exec(select(Identity).order_by(Identity.id.desc())).all()  # type: ignore[arg-type]
    return {"identities": [r.model_dump() for r in rows]}


@app.post("/api/identities")
async def add_identity(req: IdentityIn) -> dict[str, Any]:
    with get_session() as s:
        existing = s.exec(
            select(Identity).where(Identity.identity == req.identity)
        ).first()
        if existing:
            return {"identity": existing.model_dump()}
        ident = Identity(identity=req.identity, label=req.label)
        s.add(ident)
        s.commit()
        s.refresh(ident)
        return {"identity": ident.model_dump()}


@app.delete("/api/identities/{ident_id}")
async def remove_identity(ident_id: int) -> dict[str, Any]:
    with get_session() as s:
        ident = s.get(Identity, ident_id)
        if ident is None:
            raise HTTPException(404)
        s.delete(ident)
        s.commit()
    return {"removed": ident_id}


class WipeRequest(BaseModel):
    identities: list[str] = []
    pattern: str = ""
    dry_run: bool = False


@app.post("/api/wipe")
async def wipe_test_users(req: WipeRequest) -> dict[str, Any]:
    """Run scripts/cleanup_test_users.py against staging via SSH.

    We could call the cleanup logic in-process, but reusing the existing
    script means the UI and the bash wrapper stay in lockstep — one
    source of truth for which tables get wiped.
    """
    if not req.identities and not req.pattern:
        raise HTTPException(
            400, detail="Provide at least one identity or a pattern.",
        )

    args = list(req.identities)
    if req.pattern:
        args += ["--pattern", req.pattern]
    if req.dry_run:
        args += ["--dry-run"]
    args += ["--yes"]

    quoted = " ".join(shlex.quote(a) for a in args)
    remote_cmd = (
        "cd ~/madad_ai_agent && "
        "docker compose -f docker/docker-compose.yml --env-file .env exec -T "
        f"workflow python -m scripts.cleanup_test_users {quoted}"
    )
    ssh_cmd = [
        "ssh", "-i", SSH_KEY_PATH,
        "-o", "StrictHostKeyChecking=accept-new",
        f"{SSH_USER}@{SSH_HOST}",
        remote_cmd,
    ]
    log.info("running wipe: identities=%s pattern=%s dry_run=%s",
             req.identities, req.pattern, req.dry_run)

    try:
        proc = await asyncio.create_subprocess_exec(
            *ssh_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
        output = stdout.decode("utf-8", errors="replace")
        ok = proc.returncode == 0
    except asyncio.TimeoutError:
        return {
            "success": False,
            "error": "wipe timed out after 120s",
            "output": "",
        }
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc), "output": ""}

    # Persist audit.
    with get_session() as s:
        s.add(Cleanup(
            identities=json.dumps(req.identities),
            pattern=req.pattern,
            dry_run=req.dry_run,
            summary=_summarize_wipe(output),
            success=ok,
            error="" if ok else "exit code != 0",
        ))
        s.commit()

    return {
        "success": ok,
        "output": output,
        "summary": _summarize_wipe(output),
    }


def _summarize_wipe(output: str) -> str:
    """Pull out the "deleted N" totals from the cleanup script's stdout."""
    counts: dict[str, int] = {}
    for line in output.splitlines():
        m = re.search(r"✓ (\S+).+?(?:would delete|deleted)\s+(\d+)", line)
        if m:
            counts[m.group(1)] = int(m.group(2))
    return json.dumps(counts)


@app.get("/api/cleanups")
async def list_cleanups(n: int = 50) -> dict[str, Any]:
    with get_session() as s:
        rows = s.exec(
            select(Cleanup).order_by(Cleanup.id.desc()).limit(n)  # type: ignore[arg-type]
        ).all()
    return {"cleanups": [r.model_dump() for r in rows]}
