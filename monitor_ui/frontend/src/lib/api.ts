// Single API helper for the local backend. Vite proxies /api → 127.0.0.1:5001.

export type Issue = {
  at: string;
  severity: "error" | "warning" | "info" | string;
  container: string;
  rule: string;
  line: string;
};

export type Stats = {
  total: number;
  by_rule: Record<string, number>;
  by_container: Record<string, number>;
  by_severity: Record<string, number>;
  log_size_bytes: number;
};

export type ConnectionStatus = {
  tunnel: {
    open: boolean;
    local_port: number | null;
    remote: string;
    ssh: string;
    error: string | null;
  };
  monitor: any;
  ok: boolean;
};

export type Identity = {
  id: number;
  identity: string;
  label: string;
  added_at: string;
};

export type Cleanup = {
  id: number;
  at: string;
  identities: string;
  pattern: string;
  dry_run: boolean;
  summary: string;
  success: boolean;
  error: string;
};

export type WipeResult = {
  success: boolean;
  output: string;
  summary: string;
  error?: string;
};

async function request<T>(
  path: string,
  init: RequestInit = {}
): Promise<T> {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(init.headers || {}) },
    ...init,
  });
  if (!r.ok) {
    const text = await r.text();
    throw new Error(`${r.status} ${r.statusText}: ${text}`);
  }
  return r.json();
}

export const api = {
  connection: () => request<ConnectionStatus>("/api/connection"),
  reopen: () =>
    request<ConnectionStatus>("/api/connection/reopen", { method: "POST" }),

  stats: () => request<Stats>("/api/stats"),
  rules: () =>
    request<{
      regex_rules: any[];
      behavioral_rules: any[];
    }>("/api/rules"),

  issues: (opts: {
    n?: number;
    severity?: string;
    rule?: string;
    container?: string;
    source?: "live" | "history";
  }) => {
    const p = new URLSearchParams();
    if (opts.n) p.set("n", String(opts.n));
    if (opts.severity) p.set("severity", opts.severity);
    if (opts.rule) p.set("rule", opts.rule);
    if (opts.container) p.set("container", opts.container);
    if (opts.source) p.set("source", opts.source);
    return request<{ issues: Issue[]; count: number; source: string }>(
      `/api/issues?${p.toString()}`
    );
  },

  clearMonitor: () =>
    request<{ cleared: boolean }>("/api/clear", { method: "POST" }),
  clearHistory: () =>
    request<{ cleared_history: boolean }>("/api/history/clear", {
      method: "POST",
    }),

  identities: () =>
    request<{ identities: Identity[] }>("/api/identities"),
  addIdentity: (identity: string, label: string) =>
    request<{ identity: Identity }>("/api/identities", {
      method: "POST",
      body: JSON.stringify({ identity, label }),
    }),
  removeIdentity: (id: number) =>
    request<{ removed: number }>(`/api/identities/${id}`, {
      method: "DELETE",
    }),

  wipe: (identities: string[], pattern: string, dry_run: boolean) =>
    request<WipeResult>("/api/wipe", {
      method: "POST",
      body: JSON.stringify({ identities, pattern, dry_run }),
    }),

  cleanups: () =>
    request<{ cleanups: Cleanup[] }>("/api/cleanups"),
};
