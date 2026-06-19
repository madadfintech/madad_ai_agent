import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../lib/api";

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

export default function Issues() {
  const [source, setSource] = useState<"live" | "history">("live");
  const [severity, setSeverity] = useState("");
  const [rule, setRule] = useState("");
  const [container, setContainer] = useState("");

  const { data, isFetching, refetch } = useQuery({
    queryKey: ["issues-list", source, severity, rule, container],
    queryFn: () =>
      api.issues({
        n: 500,
        source,
        severity: severity || undefined,
        rule: rule || undefined,
        container: container || undefined,
      }),
    refetchInterval: source === "live" ? 5000 : false,
  });

  const { data: stats } = useQuery({
    queryKey: ["stats-for-filters"],
    queryFn: api.stats,
    refetchInterval: 10000,
  });

  const ruleOptions = Object.keys(stats?.by_rule || {});
  const containerOptions = Object.keys(stats?.by_container || {});

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold text-white">Issues</h1>
        <p className="text-sm text-mute">
          {source === "live"
            ? "Live tail from the staging monitor. Survives only until /clear."
            : "Local SQLite history — every issue we've ever pulled, survives /clear."}
        </p>
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
          {isFetching && <span className="animate-pulse">refreshing…</span>}
          <button
            onClick={() => refetch()}
            className="rounded border border-border bg-panel2 px-2 py-1"
          >
            Reload
          </button>
        </div>
      </div>

      <div className="rounded-lg border border-border bg-panel">
        <div className="grid grid-cols-12 border-b border-border px-3 py-2 text-[10px] uppercase tracking-wider text-mute">
          <div className="col-span-2">Time (UTC)</div>
          <div className="col-span-1">Severity</div>
          <div className="col-span-2">Container</div>
          <div className="col-span-2">Rule</div>
          <div className="col-span-5">Line</div>
        </div>
        <div className="max-h-[60vh] overflow-auto">
          {(data?.issues || []).map((iss, i) => (
            <div
              key={i}
              className="grid grid-cols-12 items-start border-t border-border px-3 py-2 text-[11px] font-mono first:border-t-0 hover:bg-panel2"
            >
              <div className="col-span-2 text-mute">{iss.at}</div>
              <div className="col-span-1">
                <SeverityChip severity={iss.severity} />
              </div>
              <div className="col-span-2 text-slate-300">{iss.container}</div>
              <div className="col-span-2 text-white">{iss.rule}</div>
              <div className="col-span-5 truncate text-mute" title={iss.line}>
                {iss.line}
              </div>
            </div>
          ))}
          {(data?.issues || []).length === 0 && (
            <div className="p-6 text-center text-sm text-mute">
              No issues match the current filters.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
