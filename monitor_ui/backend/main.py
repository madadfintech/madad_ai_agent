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
    monitor /clear).

    For ``source=live``, every fetched event is also INSERTED into the
    local SQLite (deduped on its content key) so AEGIS' detail / related-
    event lookup endpoints can find it by ``id``. That makes "click any
    row to investigate" work even before the 5s poll loop has caught up.
    """
    fetched_at = datetime.now(UTC).isoformat()

    if source == "live":
        data = await _monitor_get(f"/monitor/tail?n={n}")
        # Persist newcomers + resolve ids so the frontend can deep-link.
        rows: list[dict[str, Any]] = []
        with get_session() as s:
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
                key = event_key(
                    parsed["at"], parsed["container"],
                    parsed["rule"], parsed["line"],
                )
                ev = s.exec(select(Event).where(Event.key == key)).first()
                if ev is None:
                    ev = Event(
                        at=parsed["at"],
                        container=parsed["container"],
                        rule=parsed["rule"],
                        severity=parsed["severity"],
                        description="",
                        line=parsed["line"],
                        key=key,
                    )
                    s.add(ev)
                    s.flush()
                rows.append({
                    "id": ev.id,
                    "at": ev.at,
                    "severity": ev.severity,
                    "container": ev.container,
                    "rule": ev.rule,
                    "line": ev.line,
                })
            s.commit()
        return {
            "issues": rows, "count": len(rows),
            "source": "live", "fetched_at": fetched_at,
        }

    # source == "history"
    with get_session() as s:
        q = select(Event).order_by(Event.id.desc()).limit(n)  # type: ignore[arg-type]
        rows = []
        for ev in s.exec(q).all():
            if severity and ev.severity != severity:
                continue
            if rule and ev.rule != rule:
                continue
            if container and ev.container != container:
                continue
            rows.append({
                "id": ev.id,
                "at": ev.at,
                "severity": ev.severity,
                "container": ev.container,
                "rule": ev.rule,
                "line": ev.line,
            })
        return {
            "issues": rows, "count": len(rows),
            "source": "history", "fetched_at": fetched_at,
        }


# ---------------------------------------------------------------------------
# AEGIS analysis engine
# ---------------------------------------------------------------------------
# Turns a raw matched log line into a structured analysis: who/what/where/
# why, expected-vs-actual, related events in a time window, and a manual-
# action hint per rule. This is the foundation the self-healing path will
# read from once we wire the remediation layer.

# Parse `key=value` pairs out of a raw log line.
# Handles double-quoted values (with embedded spaces) AND bareword values.
_KV_RE = re.compile(
    r'(?P<key>[a-zA-Z_][a-zA-Z0-9_\.]*)='
    r'(?:"(?P<qval>[^"]*)"|(?P<val>[^\s\]\)\,]+))'
)

# Behavioral-rule decorations the monitor prepends to the raw line.
_GROUP_RE = re.compile(r"group=\[(?P<inner>[^\]]+)\]")
_COUNT_RE = re.compile(r"\bcount=(?P<count>\d+)")
_WINDOW_RE = re.compile(r"\bwindow=(?P<sec>[\d.]+)s?")
_VALUE_RE = re.compile(r"\bvalue=(?P<value>[\d.]+)")
_THRESHOLD_RE = re.compile(r"\bthreshold=(?P<threshold>[\d.]+)")
_INNER_LINE_RE = re.compile(r"line='(?P<inner>.+?)'\s*$")

# Per-rule expected-behavior + suggested-action templates. The map is
# DELIBERATELY hand-curated rather than scraped from the rule descriptions
# in rules.yml — the descriptions are short headlines for the operator UI;
# what we want here is the operator's mental model: what we expected to
# see, why this line says we didn't, and what they should poke at next.
_RULE_PLAYBOOK: dict[str, dict[str, str]] = {
    "mcp_call_terminal_failure": {
        "expected": (
            "Every MCP tool call should return 2xx within its budget. After "
            "an exhausted retry budget the workflow continues degraded — "
            "but each terminal failure is a downstream bug."
        ),
        "deviation": (
            "The tool raised after all retries. ``error_type`` + ``error`` "
            "on the line tell you whether it was a 4xx (our payload bug) "
            "or 5xx/timeout (Madad backend / cluster fault)."
        ),
        "action": (
            "Open the tool's adapter at app/services/workflow/mcp_*.py — "
            "compare the payload we sent against the cluster's tool schema. "
            "If 4xx persists, ping Ishan; if 5xx/timeout is intermittent, "
            "verify retry-disable for slow OCR tools is still in place."
        ),
    },
    "slow_invoice_extract": {
        "expected": (
            "``madad_invoices_extract_invoice_base64`` should complete in "
            "<30 s. Anything slower is Madad OCR latency."
        ),
        "deviation": (
            "The call eventually succeeded but burnt {value} ms — more than "
            "the {threshold} ms budget. With retry-disable in place each "
            "slow call is single-shot, so this only wastes wall-clock on "
            "the SME's view (not load on Madad)."
        ),
        "action": (
            "Not our bug. Confirm ``attempt=1`` on the line (retry-disable "
            "holding). If you see ``attempt=2+``, the retry-disable "
            "regressed — check ``Tools.read_only()`` in "
            "app/shared/mcp/registry.py. File OCR slowness with Madad."
        ),
    },
    "slow_invoice_submit": {
        "expected": (
            "``madad_invoices_submit_invoice_base64`` should complete in "
            "<30 s. The submit path shares OCR with the extract path."
        ),
        "deviation": (
            "Submit took {value} ms > {threshold} ms. Same root cause as "
            "slow_invoice_extract — Madad OCR latency."
        ),
        "action": (
            "Same as slow_invoice_extract — verify single-shot, file "
            "upstream."
        ),
    },
    "duplicate_outbound_send": {
        "expected": (
            "Each template should fire at most ONCE per SME action. The "
            "rule threshold is {threshold}+ within {window}s for the same "
            "(identity, template_key) — that pattern is a duplicate-send "
            "regression (concurrent webhook retry, missing inflight guard, "
            "etc.)."
        ),
        "deviation": (
            "{count} sends of the same template went out inside the window. "
            "Per-action templates that legitimately fire many times are "
            "excluded in rules.yml; this template wasn't excluded, so the "
            "duplication is real."
        ),
        "action": (
            "Identify the firing node via grep on the template_key. "
            "Suspect: a missing Redis SETNX inflight guard. See "
            "_documents_upload_loop_send / _invoice_collect_await / "
            "_maybe_settle_docs in app/services/workflow/onboarding.py for "
            "the established pattern."
        ),
    },
    "duplicate_kyc_upload": {
        "expected": (
            "Each KYC document should be uploaded EXACTLY ONCE per SME "
            "action. {threshold}+ uploads of the same filename within "
            "{window}s for one identity is a duplicate-upload bug."
        ),
        "deviation": (
            "Same (identity, filename) hit the KYC upload tool {count}+ "
            "times. Almost always: the SME re-sent the file AND we "
            "didn't dedupe AT the inflight layer."
        ),
        "action": (
            "Verify the docs:inflight:{{identity}}:{{sig}} Redis guard at "
            "the top of _documents_upload_loop_await — it should drop "
            "concurrent retries silently."
        ),
    },
    "invoice_resubmit_loop": {
        "expected": (
            "An invoice file should be submitted at most once per "
            "(identity, filename). 3+ submits of the same filename in 30 "
            "min is a re-submission loop (the QA #2 reproduction shape)."
        ),
        "deviation": (
            "Same (identity, filename) hit ``obs.invoice.submit`` "
            "{count} times in {window}s. Note: distinct-file batch rows "
            "are excluded — this means the SAME file resubmitted."
        ),
        "action": (
            "Check the permanent submitted-sig set on workflow state — "
            "state.invoice_submitted_sigs should refuse re-submits."
        ),
    },
    "dedupe_caught_duplicate": {
        "expected": (
            "Dedupe layers (inflight Redis claims, permanent submitted-sig "
            "sets, status-poller orphan-skip) should catch retries and "
            "silently drop them."
        ),
        "deviation": (
            "This is NOT a deviation. The line records a dedupe layer "
            "doing its job — a duplicate inbound or concurrent runner was "
            "rejected before producing a side effect."
        ),
        "action": (
            "No action. This is a SUCCESS signal logged at INFO severity. "
            "If you see a sustained burst of these, the upstream caller "
            "is retrying hard — worth a peek at the bridge."
        ),
    },
    "http_5xx": {
        "expected": (
            "Every HTTP request should land 2xx/3xx/4xx. 5xx means our "
            "service or a downstream raised."
        ),
        "deviation": (
            "5xx surfaced on the request path. Could be our workflow "
            "service, the MCP cluster, or Madad."
        ),
        "action": (
            "Find the request_id on the line, grep through workflow logs "
            "for the request, identify the layer that raised."
        ),
    },
    "http_4xx_non_auth": {
        "expected": (
            "Real 4xx on legitimate routes is rare; we have a real client "
            "doing something wrong OR Madad rejecting a payload. Scanner "
            "probes are excluded by the rule's exclude regex."
        ),
        "deviation": (
            "A non-auth 4xx hit a legitimate path. Check whether the path "
            "looks like ours (/workflow/inbound, /cms/...) or a probe we "
            "missed in the exclude list."
        ),
        "action": (
            "If path is /healthz or any scanner-shape we don't yet "
            "exclude, extend rules.yml. Otherwise inspect the request_id."
        ),
    },
    "http_401_403": {
        "expected": "Auth-protected routes should be hit with a valid bearer.",
        "deviation": "A 401/403 leaked through — token expiry, missing bearer, or RBAC denial.",
        "action": "Inspect the request_id + check ADMIN_API_TOKEN_EXTRAS provisioning.",
    },
    "mcp_call_failures_burst": {
        "expected": "MCP tool calls succeed at >99%. A burst of failures means cluster outage or contract drift.",
        "deviation": "{count} failures in {window}s — the cluster is either down or has shipped a breaking contract change.",
        "action": "Ping Ishan; check `D:/madad_ai_mcp_cluster` upstream/main for recent commits.",
    },
    "invoice_attempt_dedupe": {
        "expected": "Inbound dedupe should catch retried inbounds.",
        "deviation": "Not a bug — recorded for visibility.",
        "action": "None.",
    },
}


def _parse_kv(text: str) -> dict[str, str]:
    """Pull every ``key=value`` pair from a log line."""
    out: dict[str, str] = {}
    for m in _KV_RE.finditer(text):
        key = m.group("key")
        val = m.group("qval") if m.group("qval") is not None else m.group("val")
        if val is not None:
            out[key] = val
    return out


def _analyze_event(ev: Event, rule_index: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Turn a stored Event row into a structured analysis."""
    line = ev.line or ""

    # Strip the behavioral-rule metadata prefix (if any) and capture both
    # the prefix fields AND the inner raw line.
    group_match = _GROUP_RE.search(line)
    count_match = _COUNT_RE.search(line)
    window_match = _WINDOW_RE.search(line)
    value_match = _VALUE_RE.search(line)
    threshold_match = _THRESHOLD_RE.search(line)
    inner_match = _INNER_LINE_RE.search(line)

    inner_line = inner_match.group("inner") if inner_match else line
    inner_kv = _parse_kv(inner_line)

    # Group-bucket fields (e.g. "identity=+91..., template_key=...").
    group_fields: dict[str, str] = {}
    if group_match:
        for piece in group_match.group("inner").split(","):
            piece = piece.strip()
            if "=" in piece:
                k, v = piece.split("=", 1)
                group_fields[k.strip()] = v.strip()

    # Canonical "where" — service / path / request / run / identity / tool.
    where = {
        "container": ev.container,
        "service": inner_kv.get("service"),
        "method": inner_kv.get("method"),
        "path": inner_kv.get("path"),
        "request_id": inner_kv.get("request_id"),
        "run_id": inner_kv.get("run_id"),
        "identity": inner_kv.get("identity") or group_fields.get("identity"),
        "tool": inner_kv.get("tool"),
        "template_key": (
            inner_kv.get("template_key") or group_fields.get("template_key")
        ),
        "filename": inner_kv.get("filename"),
    }

    # Pull the matched rule's spec out of /monitor/rules so we can show
    # severity, description, threshold/window, etc.
    rule_spec = rule_index.get(ev.rule)

    # Synthesise the expected / deviation / action narrative.
    playbook = _RULE_PLAYBOOK.get(ev.rule, {})
    fmt_vars = {
        "count": count_match.group("count") if count_match else "?",
        "window": window_match.group("sec") if window_match else "?",
        "value": value_match.group("value") if value_match else "?",
        "threshold": (
            threshold_match.group("threshold") if threshold_match else "?"
        ),
    }
    def _fmt(template: str) -> str:
        try:
            return template.format(**fmt_vars)
        except Exception:  # noqa: BLE001
            return template

    analysis = {
        "expected_behavior": _fmt(playbook.get("expected", "")),
        "observed_deviation": _fmt(playbook.get("deviation", "")),
        "suggested_action": _fmt(playbook.get("action", "")),
    }

    return {
        "id": ev.id,
        "at": ev.at,
        "seen_at": ev.seen_at,
        "rule": ev.rule,
        "severity": ev.severity,
        "container": ev.container,
        "line": line,
        "inner_line": inner_line,
        "parsed": {
            "kv": inner_kv,
            "group": group_fields,
            "count": count_match.group("count") if count_match else None,
            "window_seconds": (
                window_match.group("sec") if window_match else None
            ),
            "value": value_match.group("value") if value_match else None,
            "threshold": (
                threshold_match.group("threshold") if threshold_match else None
            ),
            "elapsed_ms": inner_kv.get("elapsed_ms"),
            "error": inner_kv.get("error"),
            "error_type": inner_kv.get("error_type"),
        },
        "where": where,
        "rule_spec": rule_spec,
        "analysis": analysis,
    }


async def _rule_index() -> dict[str, dict[str, Any]]:
    """Pull /monitor/rules and turn it into a name → spec lookup."""
    try:
        rules = await _monitor_get("/monitor/rules")
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, dict[str, Any]] = {}
    for r in (rules.get("regex_rules") or []):
        out[r["name"]] = {**r, "kind": "regex"}
    for r in (rules.get("behavioral_rules") or []):
        out[r["name"]] = {**r, "kind": "behavioral"}
    return out


def _related_events(
    session, ev: Event, *, window_seconds: int = 120, limit: int = 30,
) -> list[dict[str, Any]]:
    """Find events near ``ev`` correlated by identity / run_id / request_id /
    template_key / filename. The correlation keys are extracted from the raw
    line; we keep this cheap by reading at most ``limit`` candidates and
    filtering in Python."""
    kv = _parse_kv(ev.line or "")
    group_match = _GROUP_RE.search(ev.line or "")
    group_fields: dict[str, str] = {}
    if group_match:
        for piece in group_match.group("inner").split(","):
            piece = piece.strip()
            if "=" in piece:
                k, v = piece.split("=", 1)
                group_fields[k.strip()] = v.strip()

    needles = {
        s
        for s in (
            kv.get("identity"),
            kv.get("run_id"),
            kv.get("request_id"),
            kv.get("template_key"),
            kv.get("tool"),
            group_fields.get("identity"),
            group_fields.get("template_key"),
        )
        if s
    }
    if not needles:
        return []

    # Time window via lexicographic comparison on ISO-8601 strings (works
    # because they're zero-padded UTC).
    try:
        anchor = datetime.fromisoformat(ev.at.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return []
    from datetime import timedelta as _td
    lo = (anchor - _td(seconds=window_seconds)).isoformat()
    hi = (anchor + _td(seconds=window_seconds)).isoformat()

    candidates = session.exec(
        select(Event)
        .where(Event.at >= lo)
        .where(Event.at <= hi)
        .order_by(Event.at)  # type: ignore[arg-type]
        .limit(500)
    ).all()

    out: list[dict[str, Any]] = []
    for cand in candidates:
        if cand.id == ev.id:
            continue
        cand_line = cand.line or ""
        if any(n in cand_line for n in needles):
            out.append({
                "id": cand.id,
                "at": cand.at,
                "rule": cand.rule,
                "severity": cand.severity,
                "container": cand.container,
                "line": cand_line[:300],
            })
        if len(out) >= limit:
            break
    return out


@app.get("/api/issues/{issue_id}")
async def issue_detail(issue_id: int) -> dict[str, Any]:
    """Rich analysis of one event: parsed fields, expected-vs-actual,
    correlated events in a ±2-min window. This is the data the AEGIS
    detail drawer renders."""
    with get_session() as s:
        ev = s.get(Event, issue_id)
        if ev is None:
            raise HTTPException(404, detail=f"issue {issue_id} not found")
        rule_index = await _rule_index()
        body = _analyze_event(ev, rule_index)
        body["related"] = _related_events(s, ev, window_seconds=120, limit=30)
        return body


@app.get("/api/correlate")
async def correlate(
    identity: str | None = None,
    run_id: str | None = None,
    request_id: str | None = None,
    template_key: str | None = None,
    minutes: int = 60,
    limit: int = 500,
) -> dict[str, Any]:
    """Pull every event matching any of the supplied correlation keys
    within the trailing window. Powers the Investigations page."""
    needles = [s for s in (identity, run_id, request_id, template_key) if s]
    if not needles:
        raise HTTPException(
            400, detail="Supply at least one of identity / run_id / "
            "request_id / template_key."
        )

    from datetime import timedelta as _td
    lo = (datetime.now(UTC) - _td(minutes=minutes)).isoformat()
    with get_session() as s:
        candidates = s.exec(
            select(Event)
            .where(Event.at >= lo)
            .order_by(Event.at)  # type: ignore[arg-type]
            .limit(limit * 5)
        ).all()
        out: list[dict[str, Any]] = []
        for cand in candidates:
            cand_line = cand.line or ""
            if any(n in cand_line for n in needles):
                out.append({
                    "id": cand.id,
                    "at": cand.at,
                    "rule": cand.rule,
                    "severity": cand.severity,
                    "container": cand.container,
                    "line": cand_line[:400],
                })
            if len(out) >= limit:
                break
    return {
        "events": out,
        "count": len(out),
        "window_minutes": minutes,
        "correlation": {
            "identity": identity,
            "run_id": run_id,
            "request_id": request_id,
            "template_key": template_key,
        },
    }


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
