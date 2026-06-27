"""Operational Visibility Service FastAPI app (Application Server container, port 8006).

Backend read APIs only (no UI): communication review log + conversation replay,
workflow visibility/history/summaries, search & audit visibility, and dashboard
aggregation (metrics + funnel). ``POST /visibility/activities`` is the ingestion
endpoint used by event consumers in production.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from fastapi.responses import HTMLResponse

from app.core.app import create_service_app

from .deps import get_visibility_service
from .enums import ActivitySource
from .models import (
    ActivityEvent,
    ConversationReplay,
    ConversationSummary,
    DashboardSnapshot,
    FunnelReport,
    MetricsSnapshot,
    WorkflowSummary,
)
from .persistence import ActivityFilter
from .service import OperationalVisibilityService

# Admin/ops service (ops.ai.madadfintech.com): gated by the admin bearer token.
# Visibility is the event CONSUMER (see consumer.py for the cross-process
# drain), so it forwards nothing — no producer lifespan here.
app = create_service_app(
    title="MADAD Operational Visibility Service", service="visibility", admin=True
)

Service = Annotated[OperationalVisibilityService, Depends(get_visibility_service)]


@app.post("/visibility/activities", response_model=ActivityEvent)
async def ingest_activity(activity: ActivityEvent, service: Service) -> ActivityEvent:
    return await service.record(activity)


@app.get("/visibility/activities", response_model=list[ActivityEvent])
async def search_activities(
    service: Service,
    source: ActivitySource | None = None,
    type: str | None = None,
    conversation_id: str | None = None,
    run_id: str | None = None,
    application_ref: str | None = None,
    identity: str | None = None,
    text: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[ActivityEvent]:
    filt = ActivityFilter(
        source=source,
        type=type,
        conversation_id=conversation_id,
        run_id=run_id,
        application_ref=application_ref,
        identity=identity,
        text=text,
    )
    return await service.list_activities(filt, limit=limit, offset=offset)


@app.get("/visibility/conversations", response_model=list[ConversationSummary])
async def list_conversations(service: Service) -> list[ConversationSummary]:
    return await service.list_conversations()


@app.get(
    "/visibility/conversations/{conversation_id}/log",
    response_model=list[ActivityEvent],
)
async def conversation_log(conversation_id: str, service: Service) -> list[ActivityEvent]:
    return await service.get_conversation_log(conversation_id)


@app.get(
    "/visibility/conversations/{conversation_id}/replay",
    response_model=ConversationReplay,
)
async def conversation_replay(conversation_id: str, service: Service) -> ConversationReplay:
    return await service.replay_conversation(conversation_id)


@app.get("/visibility/workflows", response_model=list[WorkflowSummary])
async def list_workflow_runs(service: Service) -> list[WorkflowSummary]:
    return await service.list_workflow_runs()


@app.get("/visibility/workflows/{run_id}/timeline", response_model=list[ActivityEvent])
async def workflow_timeline(run_id: str, service: Service) -> list[ActivityEvent]:
    return await service.get_workflow_timeline(run_id)


@app.get("/visibility/workflows/{run_id}/summary", response_model=WorkflowSummary)
async def workflow_summary(run_id: str, service: Service) -> WorkflowSummary:
    return await service.get_workflow_summary(run_id)


@app.get("/visibility/metrics", response_model=MetricsSnapshot)
async def metrics(service: Service) -> MetricsSnapshot:
    return service.get_metrics()


@app.get("/visibility/funnel", response_model=FunnelReport)
async def funnel(service: Service) -> FunnelReport:
    return service.get_funnel()


@app.get("/visibility/dashboard", response_model=DashboardSnapshot)
async def dashboard(service: Service) -> DashboardSnapshot:
    return await service.get_dashboard()


# -- Analytics dashboard v1 (M1 acceptance stub) ----------------------------
# Server-side rendered, single self-contained page. NO JS framework — keeps
# the staging dependency surface flat and lets ops view metrics on any
# admin laptop. Renders the same DashboardSnapshot the JSON endpoint above
# returns; the JSON one feeds the eventual rich UI we'll build out post-M1.

_DASHBOARD_HTML_TMPL = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<meta http-equiv="refresh" content="30" />
<title>MADAD — Analytics v1</title>
<style>
  :root {{
    --bg:#0b0d12; --panel:#11151c; --line:#1c2330; --ink:#e6edf3;
    --muted:#8b95a5; --accent:#3ddc97; --warn:#ffb347;
  }}
  * {{ box-sizing:border-box; }}
  body {{
    margin:0; font:14px/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;
    background:var(--bg); color:var(--ink);
  }}
  header {{
    padding:18px 28px; border-bottom:1px solid var(--line);
    display:flex; justify-content:space-between; align-items:center;
  }}
  header h1 {{ font-size:15px; font-weight:600; letter-spacing:.05em; text-transform:uppercase; margin:0; }}
  header .stamp {{ color:var(--muted); font-size:12px; font-variant-numeric:tabular-nums; }}
  main {{ padding:24px 28px; max-width:1200px; margin:0 auto; }}
  .row {{ display:grid; grid-template-columns:repeat(4,1fr); gap:16px; margin-bottom:24px; }}
  .kpi {{
    background:var(--panel); border:1px solid var(--line); border-radius:10px;
    padding:18px 20px;
  }}
  .kpi .lbl {{ color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.08em; }}
  .kpi .val {{ font-size:28px; font-weight:600; margin-top:6px; font-variant-numeric:tabular-nums; }}
  section h2 {{ font-size:13px; text-transform:uppercase; letter-spacing:.08em; color:var(--muted);
                margin:8px 0 12px; }}
  table {{ width:100%; border-collapse:collapse; background:var(--panel); border:1px solid var(--line);
           border-radius:10px; overflow:hidden; margin-bottom:24px; }}
  th, td {{ text-align:left; padding:10px 16px; border-bottom:1px solid var(--line); font-size:13px; }}
  th {{ color:var(--muted); font-weight:500; text-transform:uppercase; letter-spacing:.06em; font-size:11px; }}
  tr:last-child td {{ border-bottom:none; }}
  td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  .funnel-bar {{ background:var(--line); height:6px; border-radius:3px; position:relative; margin-top:4px; }}
  .funnel-bar > span {{ display:block; height:100%; background:var(--accent); border-radius:3px; }}
  .empty {{ color:var(--muted); padding:18px; text-align:center; }}
  footer {{ padding:18px 28px; color:var(--muted); font-size:12px; border-top:1px solid var(--line);
            display:flex; justify-content:space-between; }}
</style>
</head>
<body>
<header>
  <h1>MADAD Analytics — Dashboard v1</h1>
  <span class="stamp">auto-refresh 30s · <a href="/visibility/comms/v1" style="color:var(--accent)">Comms</a> · <a href="/visibility/dashboard" style="color:var(--accent)">raw JSON</a></span>
</header>
<main>
  <div class="row">
    <div class="kpi"><div class="lbl">Total events</div><div class="val">{total_events}</div></div>
    <div class="kpi"><div class="lbl">Conversations</div><div class="val">{conversations}</div></div>
    <div class="kpi"><div class="lbl">Workflow runs</div><div class="val">{workflow_runs}</div></div>
    <div class="kpi"><div class="lbl">Documents</div><div class="val">{documents}</div></div>
  </div>

  <section>
    <h2>Onboarding funnel</h2>
    {funnel_table}
  </section>

  <div class="row" style="grid-template-columns:1fr 1fr;">
    <section>
      <h2>Activity by source</h2>
      {by_source_table}
    </section>
    <section>
      <h2>Activity by type</h2>
      {by_type_table}
    </section>
  </div>
</main>
<footer>
  <span>MADAD FinTech · Operational Visibility</span>
  <span>v1 stub — wired to /visibility/dashboard</span>
</footer>
</body>
</html>
"""


def _funnel_table_html(funnel) -> str:
    """Render the funnel as a table with conversion bars."""
    if not funnel.stages:
        return '<div class="empty">No funnel data yet.</div>'
    rows = []
    for stage in funnel.stages:
        conv = stage.conversion
        if conv is None:
            conv_cell = "<td class='num'>—</td>"
            bar_pct = 100
        else:
            conv_cell = f"<td class='num'>{conv * 100:.1f}%</td>"
            bar_pct = max(0.0, min(100.0, conv * 100))
        rows.append(
            "<tr>"
            f"<td>{stage.label}</td>"
            f"<td class='num'>{stage.count}</td>"
            f"{conv_cell}"
            f"<td><div class='funnel-bar'><span style='width:{bar_pct:.1f}%'></span></div></td>"
            "</tr>"
        )
    return (
        "<table>"
        "<tr><th>Stage</th><th class='num'>Count</th>"
        "<th class='num'>vs. stage 1</th><th>Bar</th></tr>"
        + "".join(rows)
        + "</table>"
    )


def _breakdown_table_html(d: dict[str, int]) -> str:
    """Render a label → count breakdown table sorted by count desc."""
    if not d:
        return '<div class="empty">No data yet.</div>'
    rows = "".join(
        f"<tr><td>{k}</td><td class='num'>{v}</td></tr>"
        for k, v in sorted(d.items(), key=lambda kv: kv[1], reverse=True)
    )
    return (
        "<table><tr><th>Key</th><th class='num'>Count</th></tr>"
        + rows
        + "</table>"
    )


_COMMS_REVIEW_INDEX_TMPL = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>MADAD — Comms Review v1</title>
<style>
  :root {{
    --bg:#0b0d12; --panel:#11151c; --line:#1c2330; --ink:#e6edf3;
    --muted:#8b95a5; --accent:#3ddc97;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font:14px/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;
          background:var(--bg); color:var(--ink); }}
  header {{ padding:18px 28px; border-bottom:1px solid var(--line);
            display:flex; justify-content:space-between; align-items:center; }}
  header h1 {{ font-size:15px; font-weight:600; letter-spacing:.05em; text-transform:uppercase; margin:0; }}
  main {{ padding:24px 28px; max-width:1200px; margin:0 auto; }}
  form.search {{ display:flex; gap:12px; margin-bottom:20px; }}
  form.search input[type=text] {{
    flex:1; background:var(--panel); border:1px solid var(--line); color:var(--ink);
    padding:10px 14px; border-radius:8px; font:inherit;
  }}
  form.search button {{
    background:var(--accent); color:#0b0d12; border:none; padding:10px 18px;
    border-radius:8px; font-weight:600; cursor:pointer;
  }}
  table {{ width:100%; border-collapse:collapse; background:var(--panel); border:1px solid var(--line);
           border-radius:10px; overflow:hidden; }}
  th, td {{ text-align:left; padding:10px 16px; border-bottom:1px solid var(--line); font-size:13px; }}
  th {{ color:var(--muted); font-weight:500; text-transform:uppercase; letter-spacing:.06em; font-size:11px; }}
  tr:last-child td {{ border-bottom:none; }}
  tr:hover td {{ background:rgba(255,255,255,.02); }}
  a {{ color:var(--accent); text-decoration:none; }}
  td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  .empty {{ color:var(--muted); padding:24px; text-align:center; }}
  .badge {{ display:inline-block; padding:2px 8px; border-radius:999px; background:var(--line);
            color:var(--muted); font-size:11px; }}
  footer {{ padding:18px 28px; color:var(--muted); font-size:12px; border-top:1px solid var(--line); }}
</style>
</head>
<body>
<header>
  <h1>MADAD Comms Review — v1</h1>
  <span><a href="/visibility/dashboard/v1">Dashboard</a></span>
</header>
<main>
  <form class="search" method="get" action="/visibility/comms/v1">
    <input type="text" name="identity" placeholder="Identity (phone / email)" value="{q_identity}" />
    <input type="text" name="text" placeholder="Body contains..." value="{q_text}" />
    <button type="submit">Search</button>
  </form>
  {results}
</main>
<footer>MADAD FinTech · Operational Visibility v1 · backed by /visibility/conversations</footer>
</body>
</html>
"""


_COMMS_REVIEW_REPLAY_TMPL = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>MADAD — Replay {conversation_id}</title>
<style>
  :root {{
    --bg:#0b0d12; --panel:#11151c; --line:#1c2330; --ink:#e6edf3;
    --muted:#8b95a5; --in:#7fb1ff; --out:#3ddc97; --evt:#ffb347;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font:14px/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;
          background:var(--bg); color:var(--ink); }}
  header {{ padding:18px 28px; border-bottom:1px solid var(--line);
            display:flex; justify-content:space-between; align-items:center; }}
  header h1 {{ font-size:14px; font-weight:600; margin:0; font-variant-numeric:tabular-nums; }}
  main {{ padding:24px 28px; max-width:900px; margin:0 auto; }}
  .meta {{ color:var(--muted); font-size:12px; margin-bottom:18px; }}
  .entry {{ background:var(--panel); border:1px solid var(--line); border-radius:10px;
            padding:12px 16px; margin-bottom:10px; }}
  .entry.in {{ border-left:3px solid var(--in); }}
  .entry.out {{ border-left:3px solid var(--out); }}
  .entry.event {{ border-left:3px solid var(--evt); }}
  .entry .head {{ display:flex; justify-content:space-between; color:var(--muted); font-size:11px;
                  text-transform:uppercase; letter-spacing:.05em; margin-bottom:6px; }}
  .entry .body {{ white-space:pre-wrap; word-break:break-word; }}
  .empty {{ color:var(--muted); padding:24px; text-align:center; }}
  a {{ color:var(--out); text-decoration:none; }}
  footer {{ padding:18px 28px; color:var(--muted); font-size:12px; border-top:1px solid var(--line); }}
</style>
</head>
<body>
<header>
  <h1>Replay · {conversation_id}</h1>
  <span><a href="/visibility/comms/v1">← back to list</a></span>
</header>
<main>
  <div class="meta">{message_count} messages · {event_count} events</div>
  {entries}
</main>
<footer>MADAD FinTech · Operational Visibility v1 · backed by /visibility/conversations/{conversation_id}/replay</footer>
</body>
</html>
"""


def _comms_index_table_html(rows) -> str:
    """Render the conversation list table for the comms-review index."""
    if not rows:
        return '<div class="empty">No conversations match these filters.</div>'
    body = []
    for row in rows:
        last = row.last_activity_at.strftime("%Y-%m-%d %H:%M") if row.last_activity_at else "—"
        ident = row.identity or "<em>unknown</em>"
        body.append(
            "<tr>"
            f"<td><a href='/visibility/comms/v1/{row.conversation_id}'>{row.conversation_id}</a></td>"
            f"<td>{ident}</td>"
            f"<td><span class='badge'>{row.channel or '—'}</span></td>"
            f"<td class='num'>{row.activity_count}</td>"
            f"<td>{last}</td>"
            "</tr>"
        )
    return (
        "<table>"
        "<tr><th>Conversation</th><th>Identity</th><th>Channel</th>"
        "<th class='num'>Events</th><th>Last activity</th></tr>"
        + "".join(body)
        + "</table>"
    )


def _comms_replay_entries_html(entries) -> str:
    """Render the chronological replay entries — messages + events."""
    if not entries:
        return '<div class="empty">No activity recorded for this conversation.</div>'
    parts = []
    for entry in entries:
        when = entry.occurred_at.strftime("%Y-%m-%d %H:%M:%S")
        if entry.kind == "message":
            direction = entry.detail.get("direction") or "out"
            cls = "in" if direction == "in" else "out"
            who = "SME" if direction == "in" else "Madad"
            text = (entry.detail.get("text") or entry.summary or "").strip() or "—"
            parts.append(
                f"<div class='entry {cls}'>"
                f"<div class='head'><span>{who} · {entry.detail.get('channel') or entry.source}</span>"
                f"<span>{when}</span></div>"
                f"<div class='body'>{text}</div>"
                "</div>"
            )
        else:
            summary = entry.summary or entry.detail.get("type") or "event"
            parts.append(
                "<div class='entry event'>"
                f"<div class='head'><span>{entry.source} · event</span><span>{when}</span></div>"
                f"<div class='body'>{summary}</div>"
                "</div>"
            )
    return "".join(parts)


@app.get("/visibility/comms/v1", response_class=HTMLResponse)
async def comms_review_index(
    service: Service,
    identity: str | None = None,
    text: str | None = None,
) -> str:
    """Server-side rendered comms review index (M1 stub).

    Operational Visibility v1 surface: ops paste an SME's phone/email,
    optionally a snippet of body text, and get a list of matching
    conversations. Clicking a row drops into the replay view.
    """
    if identity or text:
        # Search activities with the supplied filter and roll up to
        # unique conversation_ids in occurrence order (most recent
        # first). The conversation summaries we render at the index
        # level use the same shape as ``list_conversations``.
        filt = ActivityFilter(identity=identity or None, text=text or None)
        acts = await service.list_activities(filt, limit=500, offset=0)
        seen: dict[str, ConversationSummary] = {}
        for a in acts:
            if not a.conversation_id or a.conversation_id in seen:
                continue
            # Lazily pull the per-conversation summary so counts/last-
            # activity-at are correct. Most ops searches return <20
            # conversations so the per-row cost is fine for the M1 stub.
            for s in await service.list_conversations():
                if s.conversation_id == a.conversation_id:
                    seen[a.conversation_id] = s
                    break
        rows = list(seen.values())
    else:
        rows = await service.list_conversations()
    return _COMMS_REVIEW_INDEX_TMPL.format(
        q_identity=identity or "",
        q_text=text or "",
        results=_comms_index_table_html(rows),
    )


@app.get("/visibility/comms/v1/{conversation_id}", response_class=HTMLResponse)
async def comms_review_replay(conversation_id: str, service: Service) -> str:
    """Server-side rendered conversation replay (M1 stub).

    Shows the chronological merge of inbound + outbound messages and
    workflow/system events for a single conversation — the "who said
    what when" audit ops needs to close a support ticket.
    """
    replay = await service.replay_conversation(conversation_id)
    return _COMMS_REVIEW_REPLAY_TMPL.format(
        conversation_id=replay.conversation_id,
        message_count=replay.message_count,
        event_count=replay.event_count,
        entries=_comms_replay_entries_html(replay.entries),
    )


@app.get("/visibility/dashboard/v1", response_class=HTMLResponse)
async def dashboard_v1_html(service: Service) -> str:
    """Server-side rendered analytics dashboard (M1 stub).

    Same data as ``GET /visibility/dashboard`` — only the rendering is
    different. Auto-refreshes every 30s via the ``http-equiv=refresh``
    meta tag, so an ops laptop just keeps the tab open during M1 demo.
    No JS framework, no separate frontend build, no external assets.
    """
    snap = await service.get_dashboard()
    return _DASHBOARD_HTML_TMPL.format(
        total_events=snap.metrics.total_events,
        conversations=snap.conversations,
        workflow_runs=snap.workflow_runs,
        documents=snap.documents,
        funnel_table=_funnel_table_html(snap.funnel),
        by_source_table=_breakdown_table_html(snap.metrics.by_source),
        by_type_table=_breakdown_table_html(snap.metrics.by_type),
    )
