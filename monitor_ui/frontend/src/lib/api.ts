// Single API helper for the local backend. Vite proxies /api → 127.0.0.1:5001.

export type Issue = {
  id?: number;
  at: string;
  severity: "error" | "warning" | "info" | string;
  container: string;
  rule: string;
  line: string;
};

export type IssueDetail = {
  id: number;
  at: string;
  seen_at: string;
  rule: string;
  severity: string;
  container: string;
  line: string;
  inner_line: string;
  parsed: {
    kv: Record<string, string>;
    group: Record<string, string>;
    count: string | null;
    window_seconds: string | null;
    value: string | null;
    threshold: string | null;
    elapsed_ms: string | null;
    error: string | null;
    error_type: string | null;
  };
  where: {
    container: string;
    service?: string | null;
    method?: string | null;
    path?: string | null;
    request_id?: string | null;
    run_id?: string | null;
    identity?: string | null;
    tool?: string | null;
    template_key?: string | null;
    filename?: string | null;
  };
  rule_spec: null | {
    name: string;
    description?: string;
    severity?: string;
    kind: "regex" | "behavioral";
    pattern?: string;
    exclude?: string;
    threshold?: number;
    window_seconds?: number;
  };
  analysis: {
    expected_behavior: string;
    observed_deviation: string;
    suggested_action: string;
  };
  related: Issue[];
};

export type CorrelationResult = {
  events: Issue[];
  count: number;
  window_minutes: number;
  correlation: {
    identity: string | null;
    run_id: string | null;
    request_id: string | null;
    template_key: string | null;
  };
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
    return request<{
      issues: Issue[];
      count: number;
      source: string;
      fetched_at: string;
    }>(`/api/issues?${p.toString()}`);
  },

  issueDetail: (id: number) => request<IssueDetail>(`/api/issues/${id}`),

  correlate: (opts: {
    identity?: string;
    run_id?: string;
    request_id?: string;
    template_key?: string;
    minutes?: number;
    limit?: number;
  }) => {
    const p = new URLSearchParams();
    if (opts.identity) p.set("identity", opts.identity);
    if (opts.run_id) p.set("run_id", opts.run_id);
    if (opts.request_id) p.set("request_id", opts.request_id);
    if (opts.template_key) p.set("template_key", opts.template_key);
    if (opts.minutes) p.set("minutes", String(opts.minutes));
    if (opts.limit) p.set("limit", String(opts.limit));
    return request<CorrelationResult>(`/api/correlate?${p.toString()}`);
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
