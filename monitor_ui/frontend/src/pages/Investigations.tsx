import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Compass, Search, AlertOctagon } from "lucide-react";
import { api, type Issue } from "../lib/api";
import IssueDetailDrawer from "../components/IssueDetailDrawer";

function SeverityChip({ severity }: { severity: string }) {
  const styles =
    severity === "error"
      ? "bg-bad/20 text-bad"
      : severity === "warning"
      ? "bg-warn/20 text-warn"
      : "bg-mute/20 text-mute";
  return (
    <span className={`rounded px-2 py-0.5 text-[10px] uppercase ${styles}`}>
      {severity}
    </span>
  );
}

// Histogram of events bucketed by minute. Renders as a row of bars so the
// operator can spot bursts at a glance — that's usually where the real
// regression lives.
function DensityChart({ events }: { events: Issue[] }) {
  const buckets = useMemo(() => {
    if (events.length === 0) return [] as { ts: string; n: number }[];
    const min = new Date(events[0].at).getTime();
    const max = new Date(events[events.length - 1].at).getTime();
    const span = Math.max(60_000, max - min);
    const slots = 60;
    const width = span / slots;
    const out: { ts: string; n: number }[] = [];
    for (let i = 0; i < slots; i++) {
      const lo = min + i * width;
      const hi = lo + width;
      const n = events.filter((e) => {
        const t = new Date(e.at).getTime();
        return t >= lo && t < hi;
      }).length;
      out.push({ ts: new Date(lo).toISOString().slice(11, 19), n });
    }
    return out;
  }, [events]);

  if (buckets.length === 0) return null;
  const peak = Math.max(1, ...buckets.map((b) => b.n));
  return (
    <div className="rounded-lg border border-border bg-panel p-3">
      <div className="mb-1 text-[10px] uppercase tracking-wider text-mute">
        Density (events per ~minute, peak = {peak})
      </div>
      <div className="flex h-16 items-end gap-[1px]">
        {buckets.map((b, i) => (
          <div
            key={i}
            title={`${b.ts} · ${b.n}`}
            style={{ height: `${(b.n / peak) * 100}%` }}
            className={`flex-1 rounded-sm ${
              b.n === 0
                ? "bg-panel2"
                : b.n / peak >= 0.66
                ? "bg-bad/70"
                : b.n / peak >= 0.33
                ? "bg-warn/70"
                : "bg-accent/70"
            }`}
          />
        ))}
      </div>
    </div>
  );
}

export default function Investigations() {
  const [params, setParams] = useSearchParams();
  const [identity, setIdentity] = useState(params.get("identity") ?? "");
  const [runId, setRunId] = useState(params.get("run_id") ?? "");
  const [requestId, setRequestId] = useState(params.get("request_id") ?? "");
  const [templateKey, setTemplateKey] = useState(
    params.get("template_key") ?? ""
  );
  // Default to 24h when typed-in directly so the operator gets a hit on
  // the first try. Click-throughs from issue detail pass anchor_at so
  // the window is centered on the source event regardless of how old it is.
  const [minutes, setMinutes] = useState<number>(
    parseInt(params.get("minutes") ?? "1440", 10)
  );
  // anchor_at survives the round-trip from the issue detail modal —
  // the URL carries it, the API uses it to center the window. Cleared
  // automatically when the operator types a fresh correlation key (no
  // anchor → use the trailing window).
  const [anchorAt, setAnchorAt] = useState<string | undefined>(
    params.get("anchor_at") ?? undefined
  );
  const [groupByRule, setGroupByRule] = useState(false);
  const [selectedId, setSelectedId] = useState<number | null>(null);

  // Keep the URL in sync so the address bar can be shared / bookmarked.
  useEffect(() => {
    const next: Record<string, string> = {};
    if (identity) next.identity = identity;
    if (runId) next.run_id = runId;
    if (requestId) next.request_id = requestId;
    if (templateKey) next.template_key = templateKey;
    if (minutes !== 1440) next.minutes = String(minutes);
    if (anchorAt) next.anchor_at = anchorAt;
    setParams(next, { replace: true });
  }, [identity, runId, requestId, templateKey, minutes, anchorAt, setParams]);

  const hasCorrelation = !!(identity || runId || requestId || templateKey);

  const { data, isFetching, refetch } = useQuery({
    queryKey: [
      "correlate",
      identity,
      runId,
      requestId,
      templateKey,
      minutes,
      anchorAt,
    ],
    queryFn: () =>
      api.correlate({
        identity: identity || undefined,
        run_id: runId || undefined,
        request_id: requestId || undefined,
        template_key: templateKey || undefined,
        minutes,
        anchor_at: anchorAt,
      }),
    enabled: hasCorrelation,
    refetchInterval: hasCorrelation ? 10000 : false,
    refetchIntervalInBackground: true,
  });

  const events = data?.events || [];
  const ruleCounts = useMemo(() => {
    const m: Record<string, number> = {};
    for (const e of events) m[e.rule] = (m[e.rule] ?? 0) + 1;
    return Object.entries(m).sort((a, b) => b[1] - a[1]);
  }, [events]);

  const grouped = useMemo(() => {
    if (!groupByRule) return null;
    const m: Record<string, Issue[]> = {};
    for (const e of events) {
      (m[e.rule] ??= []).push(e);
    }
    return Object.entries(m).sort((a, b) => b[1].length - a[1].length);
  }, [events, groupByRule]);

  return (
    <div className="space-y-4">
      <div>
        <h1 className="flex items-center gap-2 text-xl font-semibold text-ink">
          <Compass size={20} className="text-accent" /> Investigations
        </h1>
        <p className="text-sm text-mute">
          Correlate captured events by identity, run, request, or template
          to reconstruct what really happened. Click a row for the full
          analysis.
        </p>
      </div>

      <div className="grid grid-cols-1 gap-2 rounded-lg border border-border bg-panel p-3 md:grid-cols-2 lg:grid-cols-5">
        <input
          value={identity}
          onChange={(e) => {
            setIdentity(e.target.value);
            setAnchorAt(undefined);
          }}
          placeholder="Identity (e.g. +919497191690)"
          className="rounded border border-border bg-panel2 px-2 py-1.5 text-xs"
        />
        <input
          value={runId}
          onChange={(e) => {
            setRunId(e.target.value);
            setAnchorAt(undefined);
          }}
          placeholder="Run ID (run_...)"
          className="rounded border border-border bg-panel2 px-2 py-1.5 text-xs font-mono"
        />
        <input
          value={requestId}
          onChange={(e) => {
            setRequestId(e.target.value);
            setAnchorAt(undefined);
          }}
          placeholder="Request ID (req_...)"
          className="rounded border border-border bg-panel2 px-2 py-1.5 text-xs font-mono"
        />
        <input
          value={templateKey}
          onChange={(e) => {
            setTemplateKey(e.target.value);
            setAnchorAt(undefined);
          }}
          placeholder="Template key"
          className="rounded border border-border bg-panel2 px-2 py-1.5 text-xs"
        />
        <div className="flex items-center gap-2">
          <select
            value={minutes}
            onChange={(e) => setMinutes(parseInt(e.target.value, 10))}
            className="flex-1 rounded border border-border bg-panel2 px-2 py-1.5 text-xs"
          >
            <option value={15}>{anchorAt ? "±15m" : "last 15m"}</option>
            <option value={60}>{anchorAt ? "±60m" : "last 60m"}</option>
            <option value={180}>{anchorAt ? "±3h" : "last 3h"}</option>
            <option value={720}>{anchorAt ? "±12h" : "last 12h"}</option>
            <option value={1440}>{anchorAt ? "±24h" : "last 24h"}</option>
          </select>
          <button
            onClick={() => refetch()}
            disabled={!hasCorrelation}
            className="flex items-center gap-1 rounded border border-border bg-accent/20 px-2 py-1.5 text-xs text-accent hover:bg-accent/30 disabled:opacity-40"
          >
            <Search size={12} />
            Investigate
          </button>
        </div>
      </div>

      {anchorAt && (
        <div className="flex items-center justify-between rounded border border-accent/40 bg-accent/10 px-3 py-2 text-xs">
          <span>
            Window centered on event at{" "}
            <span className="font-mono text-accent">{anchorAt}</span>{" "}
            (±{minutes}m)
          </span>
          <button
            onClick={() => setAnchorAt(undefined)}
            className="rounded border border-border bg-panel2 px-2 py-0.5 text-[10px] uppercase tracking-wide text-mute hover:text-ink"
          >
            Switch to trailing window
          </button>
        </div>
      )}

      {!hasCorrelation && (
        <div className="rounded-lg border border-dashed border-border bg-panel p-8 text-center text-sm text-mute">
          Provide at least one correlation key to begin an investigation.
          <br />
          Tip: click "Investigate" links from inside an issue detail.
        </div>
      )}

      {hasCorrelation && (
        <>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-4">
            <div className="rounded-lg border border-border bg-panel p-3">
              <div className="text-[10px] uppercase tracking-wider text-mute">
                Events
              </div>
              <div className="mt-1 text-2xl font-semibold text-ink">
                {data?.count ?? 0}
              </div>
              <div className="text-[10px] text-mute">
                in last {data?.window_minutes ?? minutes}m
              </div>
            </div>
            <div className="rounded-lg border border-border bg-panel p-3">
              <div className="text-[10px] uppercase tracking-wider text-mute">
                Distinct rules
              </div>
              <div className="mt-1 text-2xl font-semibold text-ink">
                {ruleCounts.length}
              </div>
              <div className="truncate text-[10px] text-mute">
                top:{" "}
                {ruleCounts
                  .slice(0, 3)
                  .map(([r, n]) => `${r}×${n}`)
                  .join(" · ")}
              </div>
            </div>
            <div className="rounded-lg border border-border bg-panel p-3">
              <div className="text-[10px] uppercase tracking-wider text-mute">
                Errors
              </div>
              <div className="mt-1 text-2xl font-semibold text-bad">
                {events.filter((e) => e.severity === "error").length}
              </div>
              <div className="text-[10px] text-mute">
                {events.filter((e) => e.severity === "warning").length}{" "}
                warnings ·{" "}
                {events.filter((e) => e.severity === "info").length} info
              </div>
            </div>
            <div className="flex items-center justify-between rounded-lg border border-border bg-panel p-3">
              <div>
                <div className="text-[10px] uppercase tracking-wider text-mute">
                  Group by rule
                </div>
                <div className="mt-1 text-xs text-muted">
                  Collapses bursts of the same rule
                </div>
              </div>
              <input
                type="checkbox"
                checked={groupByRule}
                onChange={(e) => setGroupByRule(e.target.checked)}
                className="h-4 w-4"
              />
            </div>
          </div>

          <DensityChart events={events} />

          {isFetching && (
            <div className="text-xs text-accent">refreshing timeline…</div>
          )}

          {events.length === 0 && !isFetching && (
            <div className="rounded-lg border border-dashed border-border bg-panel p-8 text-center text-sm text-mute">
              <AlertOctagon className="mx-auto mb-2" size={20} />
              No events matched these correlation keys in the window.
            </div>
          )}

          {!groupByRule && events.length > 0 && (
            <div className="rounded-lg border border-border bg-panel">
              <div className="grid grid-cols-12 border-b border-border px-3 py-2 text-[10px] uppercase tracking-wider text-mute">
                <div className="col-span-2">Time (UTC)</div>
                <div className="col-span-1">Severity</div>
                <div className="col-span-2">Container</div>
                <div className="col-span-3">Rule</div>
                <div className="col-span-4">Line</div>
              </div>
              <div className="max-h-[55vh] overflow-auto">
                {events.map((e) => (
                  <button
                    key={e.id}
                    onClick={() => e.id && setSelectedId(e.id)}
                    className="grid w-full grid-cols-12 items-start gap-1 border-t border-border px-3 py-2 text-left text-[11px] font-mono first:border-t-0 hover:bg-panel2"
                  >
                    <div className="col-span-2 text-mute">{e.at}</div>
                    <div className="col-span-1">
                      <SeverityChip severity={e.severity} />
                    </div>
                    <div className="col-span-2 text-muted">
                      {e.container}
                    </div>
                    <div className="col-span-3 text-ink">{e.rule}</div>
                    <div className="col-span-4 truncate text-mute" title={e.line}>
                      {e.line}
                    </div>
                  </button>
                ))}
              </div>
            </div>
          )}

          {groupByRule && grouped && grouped.length > 0 && (
            <div className="space-y-3">
              {grouped.map(([rule, evs]) => (
                <div key={rule} className="rounded-lg border border-border bg-panel">
                  <div className="flex items-center justify-between border-b border-border px-3 py-2 text-xs">
                    <span className="font-semibold text-ink">{rule}</span>
                    <span className="text-mute">{evs.length} events</span>
                  </div>
                  <div className="max-h-[40vh] overflow-auto">
                    {evs.map((e) => (
                      <button
                        key={e.id}
                        onClick={() => e.id && setSelectedId(e.id)}
                        className="grid w-full grid-cols-12 items-start gap-1 border-t border-border px-3 py-1.5 text-left text-[11px] font-mono first:border-t-0 hover:bg-panel2"
                      >
                        <div className="col-span-2 text-mute">{e.at}</div>
                        <div className="col-span-1">
                          <SeverityChip severity={e.severity} />
                        </div>
                        <div className="col-span-9 truncate text-mute" title={e.line}>
                          {e.line}
                        </div>
                      </button>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}
        </>
      )}

      <IssueDetailDrawer
        issueId={selectedId}
        onClose={() => setSelectedId(null)}
        onSelectIssue={(id) => setSelectedId(id)}
      />
    </div>
  );
}
