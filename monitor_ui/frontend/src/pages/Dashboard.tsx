import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Trash2, RefreshCw, Database } from "lucide-react";
import { useEffect, useState } from "react";
import { api, Issue } from "../lib/api";

function Card({
  title,
  value,
  hint,
}: {
  title: string;
  value: string | number;
  hint?: string;
}) {
  return (
    <div className="rounded-lg border border-border bg-panel p-4">
      <div className="text-xs uppercase tracking-wider text-mute">{title}</div>
      <div className="mt-1 text-2xl font-semibold text-white">{value}</div>
      {hint && <div className="mt-1 text-xs text-mute">{hint}</div>}
    </div>
  );
}

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

function GroupTable({
  title,
  data,
}: {
  title: string;
  data: Record<string, number>;
}) {
  const rows = Object.entries(data).sort((a, b) => b[1] - a[1]).slice(0, 10);
  return (
    <div className="rounded-lg border border-border bg-panel">
      <div className="border-b border-border px-4 py-2 text-xs uppercase tracking-wider text-mute">
        {title}
      </div>
      {rows.length === 0 ? (
        <div className="p-4 text-sm text-mute">No data yet.</div>
      ) : (
        <table className="w-full text-sm">
          <tbody>
            {rows.map(([k, v]) => (
              <tr key={k} className="border-t border-border first:border-t-0">
                <td className="px-4 py-2 font-mono text-slate-200">{k}</td>
                <td className="px-4 py-2 text-right text-white">{v}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

export default function Dashboard() {
  const qc = useQueryClient();
  const { data: stats } = useQuery({
    queryKey: ["stats"],
    queryFn: api.stats,
    refetchInterval: 3000,
  });
  const { data: issues } = useQuery({
    queryKey: ["recent-issues"],
    queryFn: () => api.issues({ n: 15 }),
    refetchInterval: 3000,
  });

  const [ticker, setTicker] = useState<Issue[]>([]);
  useEffect(() => {
    // Live tail via SSE — appended to the ticker. Falls back gracefully
    // if the stream drops (the polling above keeps things fresh).
    const es = new EventSource("/api/stream");
    es.onmessage = (e) => {
      try {
        // SSE payload from the server is the dict ``repr`` — best-effort
        // parse without crashing on malformed entries.
        const v = e.data.replace(/'/g, '"');
        const obj = JSON.parse(v);
        setTicker((prev) =>
          [
            {
              at: obj.at,
              severity: obj.severity,
              container: obj.container,
              rule: obj.rule,
              line: obj.line,
            },
            ...prev,
          ].slice(0, 20)
        );
      } catch {
        // Ignore malformed lines.
      }
    };
    es.onerror = () => es.close();
    return () => es.close();
  }, []);

  async function clearMonitor() {
    if (!confirm("Clear the monitor's issues feed on staging?")) return;
    await api.clearMonitor();
    qc.invalidateQueries({ queryKey: ["stats"] });
    qc.invalidateQueries({ queryKey: ["recent-issues"] });
  }

  async function refreshAll() {
    qc.invalidateQueries({ queryKey: ["stats"] });
    qc.invalidateQueries({ queryKey: ["recent-issues"] });
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-white">Dashboard</h1>
          <p className="text-sm text-mute">
            Live signal from the staging log monitor — auto-refreshes every 3s.
          </p>
        </div>
        <div className="flex gap-2">
          <button
            onClick={refreshAll}
            className="flex items-center gap-2 rounded border border-border bg-panel2 px-3 py-1.5 text-sm hover:bg-panel"
          >
            <RefreshCw size={14} />
            Refresh
          </button>
          <button
            onClick={clearMonitor}
            className="flex items-center gap-2 rounded border border-border bg-bad/10 px-3 py-1.5 text-sm text-bad hover:bg-bad/20"
          >
            <Trash2 size={14} />
            Clear monitor
          </button>
        </div>
      </div>

      <div className="grid grid-cols-4 gap-4">
        <Card title="Total captured" value={stats?.total ?? "—"} />
        <Card
          title="Errors"
          value={stats?.by_severity?.error ?? 0}
          hint="severity=error in window"
        />
        <Card
          title="Warnings"
          value={stats?.by_severity?.warning ?? 0}
          hint="severity=warning in window"
        />
        <Card
          title="Log size"
          value={`${((stats?.log_size_bytes ?? 0) / 1024).toFixed(1)} KB`}
          hint="on the VM"
        />
      </div>

      <div className="grid grid-cols-2 gap-4">
        <GroupTable title="By rule" data={stats?.by_rule || {}} />
        <GroupTable title="By container" data={stats?.by_container || {}} />
      </div>

      <div className="rounded-lg border border-border bg-panel">
        <div className="flex items-center justify-between border-b border-border px-4 py-2">
          <div className="text-xs uppercase tracking-wider text-mute">
            Live ticker
          </div>
          <div className="flex items-center gap-1 text-[10px] text-mute">
            <Database size={12} />
            Polled + streamed
          </div>
        </div>
        <div className="max-h-[260px] overflow-auto">
          {(ticker.length > 0
            ? ticker
            : issues?.issues || []
          ).map((iss, i) => (
            <div
              key={i}
              className="flex items-center gap-3 border-t border-border px-4 py-2 text-xs font-mono first:border-t-0"
            >
              <SeverityChip severity={iss.severity} />
              <span className="text-mute">
                {iss.at?.slice(11, 19) || ""}
              </span>
              <span className="text-slate-300">{iss.container}</span>
              <span className="text-white">{iss.rule}</span>
              <span className="ml-auto truncate text-mute">{iss.line}</span>
            </div>
          ))}
          {(ticker.length === 0 && (issues?.issues || []).length === 0) && (
            <div className="p-6 text-center text-sm text-mute">
              No issues captured. Pipeline is clean ✨
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
