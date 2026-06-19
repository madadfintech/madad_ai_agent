import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, Trash2 } from "lucide-react";
import { api } from "../lib/api";

export default function SettingsPage() {
  const qc = useQueryClient();
  const { data: conn } = useQuery({
    queryKey: ["connection"],
    queryFn: api.connection,
    refetchInterval: 10000,
  });
  const { data: rules } = useQuery({
    queryKey: ["rules"],
    queryFn: api.rules,
  });

  const reopen = useMutation({
    mutationFn: api.reopen,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["connection"] }),
  });
  const clearHistory = useMutation({
    mutationFn: api.clearHistory,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["issues-list"] });
    },
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-white">Settings</h1>
        <p className="text-sm text-mute">Connection, rules, and local data.</p>
      </div>

      <div className="grid grid-cols-2 gap-4">
        <div className="rounded-lg border border-border bg-panel p-4">
          <div className="mb-3 flex items-center justify-between">
            <div className="text-xs uppercase tracking-wider text-mute">
              SSH tunnel
            </div>
            <button
              onClick={() => reopen.mutate()}
              className="flex items-center gap-1 rounded border border-border bg-panel2 px-2 py-1 text-xs hover:bg-panel"
            >
              <RefreshCw size={12} />
              Reopen
            </button>
          </div>
          <dl className="space-y-1 text-xs">
            <Row label="Status">
              <span
                className={conn?.tunnel.open ? "text-good" : "text-bad"}
              >
                {conn?.tunnel.open ? "open" : "down"}
              </span>
            </Row>
            <Row label="Local port">
              <span className="font-mono">
                {conn?.tunnel.local_port ?? "—"}
              </span>
            </Row>
            <Row label="Remote">
              <span className="font-mono">{conn?.tunnel.remote}</span>
            </Row>
            <Row label="SSH">
              <span className="font-mono">{conn?.tunnel.ssh}</span>
            </Row>
            {conn?.tunnel.error && (
              <Row label="Error">
                <span className="text-bad">{conn.tunnel.error}</span>
              </Row>
            )}
          </dl>
        </div>

        <div className="rounded-lg border border-border bg-panel p-4">
          <div className="mb-3 flex items-center justify-between">
            <div className="text-xs uppercase tracking-wider text-mute">
              Local history (SQLite)
            </div>
            <button
              onClick={() => {
                if (confirm("Drop local event history?")) clearHistory.mutate();
              }}
              className="flex items-center gap-1 rounded border border-border bg-bad/10 px-2 py-1 text-xs text-bad hover:bg-bad/20"
            >
              <Trash2 size={12} />
              Clear history
            </button>
          </div>
          <p className="text-xs text-mute">
            The local SQLite stores every event we've pulled, even after the
            remote monitor was cleared. Use this only if the history is
            actually irrelevant going forward — there's no undo.
          </p>
        </div>
      </div>

      <div className="rounded-lg border border-border bg-panel">
        <div className="border-b border-border px-4 py-2 text-xs uppercase tracking-wider text-mute">
          Loaded rules
        </div>
        <div className="grid grid-cols-2 gap-0">
          <div className="border-r border-border p-3">
            <div className="mb-2 text-xs font-semibold text-slate-300">
              Regex rules ({rules?.regex_rules.length ?? 0})
            </div>
            <ul className="space-y-1">
              {(rules?.regex_rules || []).map((r) => (
                <li
                  key={r.name}
                  className="rounded bg-panel2 px-2 py-1 font-mono text-[11px]"
                >
                  <span className="text-white">{r.name}</span>
                  <span className="ml-2 text-mute">{r.severity}</span>
                </li>
              ))}
            </ul>
          </div>
          <div className="p-3">
            <div className="mb-2 text-xs font-semibold text-slate-300">
              Behavioral rules ({rules?.behavioral_rules.length ?? 0})
            </div>
            <ul className="space-y-1">
              {(rules?.behavioral_rules || []).map((r) => (
                <li
                  key={r.name}
                  className="rounded bg-panel2 px-2 py-1 font-mono text-[11px]"
                >
                  <span className="text-white">{r.name}</span>
                  <span className="ml-2 text-mute">
                    {r.type} · {r.severity}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </div>
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex justify-between">
      <dt className="text-mute">{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}
