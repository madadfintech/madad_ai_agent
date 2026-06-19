"""Madad log monitor — tails container logs, filters issues, writes them
to a host-mounted file, and exposes a small admin API for tail/clear/stats.

Design choices:
* Tails ``docker logs -f`` for each watched container via subprocess so
  the monitor needs only the docker.sock mount (no Docker SDK dep).
* A rule engine driven by ``rules.yml`` decides what counts as an
  "issue" — easy to edit without redeploy (mount it and SIGHUP, or just
  restart the container).
* Matches are appended to ``ISSUES_LOG_PATH`` (host-mounted) AND held in
  a small in-memory ring buffer the API serves.
* Notifications stub: when ``NOTIFY_WEBHOOK_URL`` is set, every matched
  issue is POSTed there (Slack/Discord/Pushover-compatible JSON). Empty
  by default — ship the MVP, layer notifications later.

Endpoints (all behind the existing admin bearer token):
* ``GET  /monitor/health``          - service health
* ``GET  /monitor/tail?n=200``      - last N matched lines (from file)
* ``POST /monitor/clear``           - truncate the issues log
* ``GET  /monitor/stats``           - counts by rule + by container
* ``GET  /monitor/stream``          - SSE live tail (nice-to-have)
* ``GET  /monitor/rules``           - introspect the loaded rules
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse

from behavioral import evaluate_all, load_behavioral_rules

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONTAINERS = [
    c.strip()
    for c in os.environ.get(
        "WATCH_CONTAINERS",
        "madad_workflow,madad_celery_worker,madad_cms,madad_communication,"
        "madad_document,madad_nudge,madad_visibility",
    ).split(",")
    if c.strip()
]
ISSUES_LOG_PATH = Path(os.environ.get("ISSUES_LOG_PATH", "/data/issues.log"))
RULES_PATH = Path(os.environ.get("RULES_PATH", "/app/rules.yml"))
ADMIN_TOKEN = os.environ.get("ADMIN_API_TOKEN", "")
NOTIFY_WEBHOOK_URL = os.environ.get("NOTIFY_WEBHOOK_URL", "").strip()
BUFFER_SIZE = int(os.environ.get("RING_BUFFER_SIZE", "1000"))
ROTATE_BYTES = int(os.environ.get("ROTATE_BYTES", str(50 * 1024 * 1024)))  # 50MB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("madad.log_monitor")


# ---------------------------------------------------------------------------
# Rule engine
# ---------------------------------------------------------------------------


def _load_rules() -> list[dict[str, Any]]:
    """Load rules from rules.yml. Each rule is a dict with at minimum a
    ``name`` and ``pattern``; an optional ``severity`` field defaults
    to "warning". Compiled patterns are cached on the rule itself."""
    raw = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    rules: list[dict[str, Any]] = []
    for entry in (raw or {}).get("rules", []):
        name = entry.get("name")
        pattern = entry.get("pattern")
        if not name or not pattern:
            log.warning("skipping invalid rule: %s", entry)
            continue
        try:
            entry["_re"] = re.compile(pattern)
        except re.error as exc:
            log.warning("invalid regex for rule %s: %s", name, exc)
            continue
        entry.setdefault("severity", "warning")
        entry.setdefault("description", "")
        rules.append(entry)
    log.info("loaded %d rules from %s", len(rules), RULES_PATH)
    return rules


RULES: list[dict[str, Any]] = []
BEHAVIORAL_RULES: list = []
RING: deque[dict[str, Any]] = deque(maxlen=BUFFER_SIZE)
LIVE_QUEUES: set[asyncio.Queue[dict[str, Any]]] = set()


def match_line(line: str) -> dict[str, Any] | None:
    """Return the first rule that matches, or None."""
    for rule in RULES:
        if rule["_re"].search(line):
            return rule
    return None


def _load_behavioral() -> list:
    """Load behavioral rules from the same rules.yml file."""
    try:
        raw = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        log.warning("behavioral rules: failed to read %s: %s", RULES_PATH, exc)
        return []
    return load_behavioral_rules(raw.get("behavioral_rules") or [])


# ---------------------------------------------------------------------------
# Tailer
# ---------------------------------------------------------------------------


async def _rotate_if_needed() -> None:
    try:
        if ISSUES_LOG_PATH.exists() and ISSUES_LOG_PATH.stat().st_size > ROTATE_BYTES:
            rotated = ISSUES_LOG_PATH.with_suffix(
                f".{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.log"
            )
            ISSUES_LOG_PATH.rename(rotated)
            log.info("rotated issues log to %s", rotated.name)
    except OSError as exc:  # noqa: BLE001
        log.warning("rotation skipped: %s", exc)


async def _append_issue(entry: dict[str, Any]) -> None:
    """Write an issue entry to the host-mounted log AND the ring buffer."""
    await _rotate_if_needed()
    ISSUES_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = (
        f"{entry['at']} | {entry['severity']:>7} | {entry['container']:<26} "
        f"| {entry['rule']:<28} | {entry['line']}\n"
    )
    try:
        with ISSUES_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError as exc:  # noqa: BLE001
        log.warning("failed to append issue to log: %s", exc)

    RING.append(entry)
    # Fan out to any live SSE consumers (non-blocking).
    for q in list(LIVE_QUEUES):
        try:
            q.put_nowait(entry)
        except asyncio.QueueFull:
            pass

    # Notification stub — defer to a background task so a slow webhook
    # never blocks ingestion.
    if NOTIFY_WEBHOOK_URL:
        asyncio.create_task(_notify(entry))


async def _notify(entry: dict[str, Any]) -> None:
    """POST a matched issue to the configured webhook. JSON shape is
    Slack/Discord/Pushover-compatible — most webhook tools accept it as-is."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                NOTIFY_WEBHOOK_URL,
                json={
                    "text": (
                        f"[{entry['severity']}] {entry['container']} "
                        f"({entry['rule']}): {entry['line'][:400]}"
                    ),
                    "username": "madad-monitor",
                    "icon_emoji": ":warning:",
                    "raw": entry,
                },
            )
    except (httpx.HTTPError, Exception) as exc:  # noqa: BLE001
        log.warning("notify webhook failed: %s", exc)


async def _tail_container(container: str) -> None:
    """Tail ``docker logs -f`` for one container. If the process exits
    (container restart, transient docker hiccup), backoff + reconnect.

    Note: ``docker logs`` writes structured app output on STDERR for
    many of our services — pipe both streams so nothing is missed."""

    backoff = 1.0
    while True:
        log.info("starting tail: %s", container)
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "logs",
                "-f",
                "--tail=10",
                container,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except (FileNotFoundError, OSError) as exc:
            log.warning("docker exec failed for %s: %s — sleeping", container, exc)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
            continue

        try:
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                # 1) Line-by-line regex rules (crashes / typed errors).
                rule = match_line(line)
                if rule is not None:
                    entry = {
                        "at": datetime.now(UTC).isoformat(),
                        "container": container,
                        "rule": rule["name"],
                        "severity": rule.get("severity", "warning"),
                        "description": rule.get("description", ""),
                        "line": line,
                    }
                    await _append_issue(entry)
                # 2) Behavioral rules (stateful — track windows / counts /
                # values across lines).
                for alert in evaluate_all(BEHAVIORAL_RULES, line):
                    behavioral_entry = {
                        "at": datetime.now(UTC).isoformat(),
                        "container": container,
                        "rule": alert["rule"],
                        "severity": alert.get("severity", "warning"),
                        "description": alert.get("description", ""),
                        "line": _format_behavioral_summary(alert),
                    }
                    await _append_issue(behavioral_entry)
        except asyncio.CancelledError:
            log.info("tail cancelled: %s", container)
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise
        except Exception as exc:  # noqa: BLE001 — keep the tailer alive
            log.warning("tail stream error for %s: %s", container, exc)

        # Stream ended (container restart?) — wait, then reconnect.
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30.0)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def _require_admin(authorization: str | None) -> None:
    if not ADMIN_TOKEN:
        return  # No token configured — open mode (dev/staging only).
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, detail="missing bearer token")
    if authorization[7:] != ADMIN_TOKEN:
        raise HTTPException(403, detail="invalid bearer token")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


app = FastAPI(title="Madad Log Monitor", version="0.1.0")


def _format_behavioral_summary(alert: dict[str, Any]) -> str:
    """One-line summary for the issues log when a behavioral rule fires."""
    parts = []
    if "group_key" in alert:
        parts.append(f"group=[{alert['group_key']}]")
    if "count_in_window" in alert:
        parts.append(
            f"count={alert['count_in_window']} window={alert['window_seconds']}s"
        )
    if "value" in alert:
        parts.append(f"value={alert['value']} threshold={alert['threshold']}")
    if alert.get("matched_line"):
        parts.append(f"line='{alert['matched_line']}'")
    return " | ".join(parts)


@app.on_event("startup")
async def _startup() -> None:
    global RULES, BEHAVIORAL_RULES
    RULES = _load_rules()
    BEHAVIORAL_RULES = _load_behavioral()
    ISSUES_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    for container in CONTAINERS:
        asyncio.create_task(_tail_container(container))
    log.info(
        "monitor started: watching %d containers, %d regex rules, "
        "%d behavioral rules, log=%s",
        len(CONTAINERS),
        len(RULES),
        len(BEHAVIORAL_RULES),
        ISSUES_LOG_PATH,
    )


@app.get("/monitor/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "containers": CONTAINERS,
        "rules": len(RULES),
        "buffer": len(RING),
        "log_file": str(ISSUES_LOG_PATH),
        "log_size_bytes": (
            ISSUES_LOG_PATH.stat().st_size if ISSUES_LOG_PATH.exists() else 0
        ),
        "notify_webhook_configured": bool(NOTIFY_WEBHOOK_URL),
    }


@app.get("/monitor/rules")
async def rules(authorization: str | None = Header(None)) -> dict[str, Any]:
    _require_admin(authorization)
    return {
        "regex_rules": [
            {
                "name": r["name"],
                "pattern": r["pattern"],
                "severity": r.get("severity"),
                "description": r.get("description"),
            }
            for r in RULES
        ],
        "behavioral_rules": [
            {
                "name": r.name,
                "type": type(r).__name__,
                "severity": r.severity,
                "description": r.description,
                **(
                    {
                        "threshold": r.threshold,
                        "window_seconds": r.window_seconds,
                    } if hasattr(r, "window_seconds") else {}
                ),
                **(
                    {"threshold": r.threshold}
                    if hasattr(r, "_value_pattern") else {}
                ),
            }
            for r in BEHAVIORAL_RULES
        ],
    }


@app.get("/monitor/tail")
async def tail(
    n: int = 200,
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    _require_admin(authorization)
    n = max(1, min(n, BUFFER_SIZE))
    if not ISSUES_LOG_PATH.exists():
        return {"lines": [], "count": 0}
    # Read tail — small files are fine to slurp.
    lines = ISSUES_LOG_PATH.read_text(encoding="utf-8").splitlines()
    return {"lines": lines[-n:], "count": min(len(lines), n)}


@app.post("/monitor/clear")
async def clear(authorization: str | None = Header(None)) -> dict[str, Any]:
    _require_admin(authorization)
    rotated_size = (
        ISSUES_LOG_PATH.stat().st_size if ISSUES_LOG_PATH.exists() else 0
    )
    ISSUES_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    ISSUES_LOG_PATH.write_text("", encoding="utf-8")
    RING.clear()
    return {
        "cleared": True,
        "rotated_size_bytes": rotated_size,
        "at": datetime.now(UTC).isoformat(),
    }


@app.get("/monitor/stats")
async def stats(authorization: str | None = Header(None)) -> dict[str, Any]:
    _require_admin(authorization)
    by_rule: dict[str, int] = {}
    by_container: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    total = 0
    if ISSUES_LOG_PATH.exists():
        for raw in ISSUES_LOG_PATH.read_text(encoding="utf-8").splitlines():
            parts = [p.strip() for p in raw.split(" | ")]
            if len(parts) < 4:
                continue
            total += 1
            by_severity[parts[1]] = by_severity.get(parts[1], 0) + 1
            by_container[parts[2]] = by_container.get(parts[2], 0) + 1
            by_rule[parts[3]] = by_rule.get(parts[3], 0) + 1
    return {
        "total": total,
        "by_rule": dict(sorted(by_rule.items(), key=lambda kv: -kv[1])),
        "by_container": dict(sorted(by_container.items(), key=lambda kv: -kv[1])),
        "by_severity": by_severity,
        "log_size_bytes": (
            ISSUES_LOG_PATH.stat().st_size if ISSUES_LOG_PATH.exists() else 0
        ),
    }


@app.get("/monitor/stream")
async def stream(authorization: str | None = Header(None)) -> StreamingResponse:
    _require_admin(authorization)

    async def gen():
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=200)
        LIVE_QUEUES.add(q)
        try:
            # Replay the last 50 ring entries so the consumer sees recent context.
            for recent in list(RING)[-50:]:
                yield f"data: {recent}\n\n"
            while True:
                entry = await q.get()
                yield f"data: {entry}\n\n"
        finally:
            LIVE_QUEUES.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream")
