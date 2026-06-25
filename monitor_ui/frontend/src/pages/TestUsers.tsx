import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2, Play } from "lucide-react";
import { useState } from "react";
import { api, Identity } from "../lib/api";

export default function TestUsers() {
  const qc = useQueryClient();
  const { data: idents } = useQuery({
    queryKey: ["identities"],
    queryFn: api.identities,
  });
  const { data: cleanups } = useQuery({
    queryKey: ["cleanups"],
    queryFn: api.cleanups,
    refetchInterval: 5000,
  });

  const [newIdent, setNewIdent] = useState("");
  const [newLabel, setNewLabel] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [adhoc, setAdhoc] = useState("");
  const [pattern, setPattern] = useState("");
  const [dryRun, setDryRun] = useState(true);
  const [result, setResult] = useState<{
    output?: string;
    success?: boolean;
    summary?: string;
  } | null>(null);

  const addIdent = useMutation({
    mutationFn: () => api.addIdentity(newIdent.trim(), newLabel.trim()),
    onSuccess: () => {
      setNewIdent("");
      setNewLabel("");
      qc.invalidateQueries({ queryKey: ["identities"] });
    },
  });
  const removeIdent = useMutation({
    mutationFn: (id: number) => api.removeIdentity(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["identities"] }),
  });

  const wipe = useMutation({
    mutationFn: () =>
      api.wipe(
        [
          ...Array.from(selected),
          ...adhoc
            .split(/[\s,]+/)
            .map((s) => s.trim())
            .filter(Boolean),
        ],
        pattern.trim(),
        dryRun
      ),
    onSuccess: (r) => {
      setResult(r);
      qc.invalidateQueries({ queryKey: ["cleanups"] });
    },
    onError: (err: Error) =>
      setResult({ success: false, output: err.message, summary: "" }),
  });

  function toggle(ident: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(ident) ? next.delete(ident) : next.add(ident);
      return next;
    });
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-ink">Test Users</h1>
        <p className="text-sm text-mute">
          Saved identities · ad-hoc input · pattern wipe · audit history.
        </p>
      </div>

      <div className="grid grid-cols-3 gap-4">
        {/* Saved identities */}
        <div className="rounded-lg border border-border bg-panel">
          <div className="flex items-center justify-between border-b border-border px-4 py-2">
            <div className="text-xs uppercase tracking-wider text-mute">
              Saved identities
            </div>
            <span className="text-xs text-mute">
              {(idents?.identities || []).length}
            </span>
          </div>
          <div className="max-h-[300px] overflow-auto">
            {(idents?.identities || []).map((id: Identity) => (
              <label
                key={id.id}
                className="flex cursor-pointer items-center gap-3 border-t border-border px-4 py-2 text-xs first:border-t-0 hover:bg-panel2"
              >
                <input
                  type="checkbox"
                  checked={selected.has(id.identity)}
                  onChange={() => toggle(id.identity)}
                  className="accent-accent"
                />
                <div className="flex-1">
                  <div className="font-mono text-ink">{id.identity}</div>
                  {id.label && (
                    <div className="text-mute">{id.label}</div>
                  )}
                </div>
                <button
                  onClick={(e) => {
                    e.preventDefault();
                    removeIdent.mutate(id.id);
                  }}
                  className="text-mute hover:text-bad"
                >
                  <Trash2 size={14} />
                </button>
              </label>
            ))}
            {(idents?.identities || []).length === 0 && (
              <div className="p-4 text-sm text-mute">
                No saved identities — add one below.
              </div>
            )}
          </div>
          <div className="border-t border-border p-3">
            <div className="grid grid-cols-1 gap-2">
              <input
                value={newIdent}
                onChange={(e) => setNewIdent(e.target.value)}
                placeholder="+919497191690"
                className="rounded border border-border bg-panel2 px-2 py-1 text-xs"
              />
              <input
                value={newLabel}
                onChange={(e) => setNewLabel(e.target.value)}
                placeholder="label (optional)"
                className="rounded border border-border bg-panel2 px-2 py-1 text-xs"
              />
              <button
                onClick={() => addIdent.mutate()}
                disabled={!newIdent.trim()}
                className="flex items-center justify-center gap-1 rounded border border-border bg-panel2 px-2 py-1 text-xs hover:bg-panel disabled:opacity-50"
              >
                <Plus size={12} /> Add
              </button>
            </div>
          </div>
        </div>

        {/* Wipe form */}
        <div className="col-span-2 space-y-4">
          <div className="rounded-lg border border-border bg-panel p-4">
            <div className="mb-3 text-xs uppercase tracking-wider text-mute">
              Wipe options
            </div>
            <div className="space-y-3">
              <div>
                <div className="mb-1 text-xs text-mute">Selected</div>
                <div className="rounded border border-border bg-panel2 px-2 py-1 text-xs font-mono">
                  {selected.size === 0 ? (
                    <span className="text-mute">(none)</span>
                  ) : (
                    Array.from(selected).join("  ·  ")
                  )}
                </div>
              </div>
              <div>
                <div className="mb-1 text-xs text-mute">
                  Ad-hoc identities (space or comma separated)
                </div>
                <input
                  value={adhoc}
                  onChange={(e) => setAdhoc(e.target.value)}
                  placeholder="+919497191690 +918287611995"
                  className="w-full rounded border border-border bg-panel2 px-2 py-1 text-xs font-mono"
                />
              </div>
              <div>
                <div className="mb-1 text-xs text-mute">
                  Pattern (SQL LIKE — e.g. <code>+91%</code>)
                </div>
                <input
                  value={pattern}
                  onChange={(e) => setPattern(e.target.value)}
                  placeholder="+91%"
                  className="w-full rounded border border-border bg-panel2 px-2 py-1 text-xs font-mono"
                />
              </div>
              <div className="flex items-center justify-between pt-2">
                <label className="flex items-center gap-2 text-xs">
                  <input
                    type="checkbox"
                    checked={dryRun}
                    onChange={(e) => setDryRun(e.target.checked)}
                    className="accent-accent"
                  />
                  Dry run (count only, don't delete)
                </label>
                <button
                  onClick={() => wipe.mutate()}
                  disabled={
                    wipe.isPending ||
                    (selected.size === 0 && !adhoc.trim() && !pattern.trim())
                  }
                  className={`flex items-center gap-2 rounded px-3 py-1.5 text-sm ${
                    dryRun
                      ? "border border-border bg-panel2 hover:bg-panel"
                      : "bg-bad/20 text-bad hover:bg-bad/30"
                  } disabled:opacity-50`}
                >
                  <Play size={14} />
                  {wipe.isPending ? "Running…" : dryRun ? "Dry run" : "Wipe"}
                </button>
              </div>
            </div>
          </div>

          {result && (
            <div className="rounded-lg border border-border bg-panel">
              <div className="border-b border-border px-4 py-2 text-xs uppercase tracking-wider text-mute">
                Last run
              </div>
              <pre className="max-h-[260px] overflow-auto p-4 text-[11px] font-mono text-muted">
                {result.output}
              </pre>
            </div>
          )}
        </div>
      </div>

      {/* Cleanup history */}
      <div className="rounded-lg border border-border bg-panel">
        <div className="border-b border-border px-4 py-2 text-xs uppercase tracking-wider text-mute">
          Cleanup history
        </div>
        <div className="max-h-[260px] overflow-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="bg-panel2 text-mute">
                <th className="px-3 py-2 text-left">When</th>
                <th className="px-3 py-2 text-left">Identities</th>
                <th className="px-3 py-2 text-left">Pattern</th>
                <th className="px-3 py-2 text-left">Mode</th>
                <th className="px-3 py-2 text-left">Summary</th>
              </tr>
            </thead>
            <tbody>
              {(cleanups?.cleanups || []).map((c) => (
                <tr key={c.id} className="border-t border-border">
                  <td className="px-3 py-2 text-mute">{c.at.slice(0, 19)}</td>
                  <td className="px-3 py-2 font-mono text-ink">
                    {JSON.parse(c.identities || "[]").join(", ") || "(none)"}
                  </td>
                  <td className="px-3 py-2 font-mono">{c.pattern || "—"}</td>
                  <td className="px-3 py-2">
                    {c.dry_run ? (
                      <span className="text-mute">dry</span>
                    ) : (
                      <span className="text-bad">live</span>
                    )}
                  </td>
                  <td className="px-3 py-2 font-mono text-mute">
                    {c.summary || "—"}
                  </td>
                </tr>
              ))}
              {(cleanups?.cleanups || []).length === 0 && (
                <tr>
                  <td
                    colSpan={5}
                    className="px-3 py-4 text-center text-mute"
                  >
                    No cleanups recorded yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
