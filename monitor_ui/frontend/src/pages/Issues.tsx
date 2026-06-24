import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { api } from "../lib/api";
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

function freshness(ts: string | undefined): string {
  if (!ts) return "—";
  const delta = Math.max(0, (Date.now() - new Date(ts).getTime()) / 1000);
  if (delta < 1) return "just now";
  if (delta < 60) return `${Math.floor(delta)}s ago`;
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
  return `${Math.floor(delta / 3600)}h ago`;
}

export default function Issues() {
  const qc = useQueryClient();
  const [source, setSource] = useState<"live" | "history">("live");
  const [severity, setSeverity] = useState("");
  const [rule, setRule] = useState("");
  const [container, setContainer] = useState("");
  const [selectedId, setSelectedId] = useState<number | null>(null);
  // Recompute "Xs ago" once per second without re-fetching.
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  const { data, isFetching, refetch, dataUpdatedAt } = useQuery({
    queryKey: ["issues-list", source, severity, rule, container],
    queryFn: () =>
      api.issues({
        n: 500,
        source,
        severity: severity || undefined,
        rule: rule || undefined,
        container: container || undefined,
      }),
    // Hard 5 s auto-refresh on live; on history we still poll every 15 s
    // because the local SQLite is being filled by the backend's poll loop.
    refetchInterval: source === "live" ? 5000 : 15000,
    // Keep refetching while the tab is in the background — operators leave
    // this dashboard up next to their terminal and expect it to stay live.
    refetchIntervalInBackground: true,
  });

  const { data: stats } = useQuery({
    queryKey: ["stats-for-filters"],
    queryFn: api.stats,
    refetchInterval: 10000,
    refetchIntervalInBackground: true,
  });

  const ruleOptions = Object.keys(stats?.by_rule || {});
  const containerOptions = Object.keys(stats?.by_container || {});

  const lastFetched = data?.fetched_at
    ? data.fetched_at
    : dataUpdatedAt
    ? new Date(dataUpdatedAt).toISOString()
    : undefined;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-white">Issues</h1>
          <p className="text-sm text-mute">
            {source === "live"
              ? "Live tail from the staging monitor — auto-refreshing every 5s. Survives only until /clear."
              : "Local SQLite history — every issue we've ever pulled, survives /clear."}
          </p>
        </div>
        <div className="text-right text-xs text-mute">
          <div>
            Last refresh:{" "}
            <span className="text-slate-300">{freshness(lastFetched)}</span>
          </div>
          <div className="opacity-60">Source: {data?.source ?? source}</div>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2 rounded-lg border border-border bg-panel p-3">
        <div className="flex overflow-hidden rounded border border-border">
          <button
            onClick={() => setSource("live")}
            className={`px-3 py-1 text-xs ${
              source === "live"
                ? "bg-accent/20 text-accent"
                : "bg-panel2 text-mute"
            }`}
          >
            Live
          </button>
          <button
            onClick={() => setSource("history")}
            className={`px-3 py-1 text-xs ${
              source === "history"
                ? "bg-accent/20 text-accent"
                : "bg-panel2 text-mute"
            }`}
          >
            History
          </button>
        </div>

        <select
          className="rounded border border-border bg-panel2 px-2 py-1 text-xs"
          value={severity}
          onChange={(e) => setSeverity(e.target.value)}
        >
          <option value="">All severities</option>
          <option value="error">error</option>
          <option value="warning">warning</option>
          <option value="info">info</option>
        </select>
        <select
          className="rounded border border-border bg-panel2 px-2 py-1 text-xs"
          value={rule}
          onChange={(e) => setRule(e.target.value)}
        >
          <option value="">All rules</option>
          {ruleOptions.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
        <select
          className="rounded border border-border bg-panel2 px-2 py-1 text-xs"
          value={container}
          onChange={(e) => setContainer(e.target.value)}
        >
          <option value="">All containers</option>
          {containerOptions.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>

        <div className="ml-auto flex items-center gap-2 text-xs text-mute">
          <span>{data?.count ?? 0} matches</span>
          {isFetching && (
            <span className="animate-pulse text-accent">refreshing…</span>
          )}
          <button
            onClick={() => {
              refetch();
              qc.invalidateQueries({ queryKey: ["stats-for-filters"] });
            }}
            className="flex items-center gap-1 rounded border border-border bg-panel2 px-2 py-1 text-xs text-slate-200 hover:bg-panel2/80"
            title="Force a refresh now"
          >
            <RefreshCw
              size={12}
              className={isFetching ? "animate-spin" : ""}
            />
            Refresh
          </button>
        </div>
      </div>

      <div className="rounded-lg border border-border bg-panel">
        <div className="grid grid-cols-12 border-b border-border px-3 py-2 text-[10px] uppercase tracking-wider text-mute">
          <div className="col-span-2">Time (UTC)</div>
          <div className="col-span-1">Severity</div>
          <div className="col-span-2">Container</div>
          <div className="col-span-3">Rule</div>
          <div className="col-span-4">Line</div>
        </div>
        <div className="max-h-[60vh] overflow-auto">
          {(data?.issues || []).map((iss, i) => (
            <button
              key={iss.id ?? `${iss.at}-${i}`}
              onClick={() => iss.id && setSelectedId(iss.id)}
              disabled={!iss.id}
              className="grid w-full grid-cols-12 items-start gap-1 border-t border-border px-3 py-2 text-left text-[11px] font-mono first:border-t-0 hover:bg-panel2 disabled:cursor-default"
              title="Click for full investigation"
            >
              <div className="col-span-2 text-mute">{iss.at}</div>
              <div className="col-span-1">
                <SeverityChip severity={iss.severity} />
              </div>
              <div className="col-span-2 text-slate-300">{iss.container}</div>
              <div className="col-span-3 text-white">{iss.rule}</div>
              <div className="col-span-4 truncate text-mute" title={iss.line}>
                {iss.line}
              </div>
            </button>
          ))}
          {(data?.issues || []).length === 0 && (
            <div className="p-6 text-center text-sm text-mute">
              No issues match the current filters.
            </div>
          )}
        </div>
      </div>

      <IssueDetailDrawer
        issueId={selectedId}
        onClose={() => setSelectedId(null)}
        onSelectIssue={(id) => setSelectedId(id)}
      />
    </div>
  );
}
