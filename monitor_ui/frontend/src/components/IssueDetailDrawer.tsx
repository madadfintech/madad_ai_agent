import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  X,
  AlertTriangle,
  Activity,
  Wrench,
  Target,
  MapPin,
  FileText,
  ListTree,
} from "lucide-react";
import { api, type Issue, type IssueDetail } from "../lib/api";

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

function Section({
  icon,
  title,
  accent,
  children,
}: {
  icon: JSX.Element;
  title: string;
  accent?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-lg border border-border bg-panel p-4">
      <div className="mb-3 flex items-center gap-2 text-[11px] uppercase tracking-wider text-mute">
        <span className={accent ?? "text-accent"}>{icon}</span>
        <span>{title}</span>
      </div>
      {children}
    </section>
  );
}

function KeyVal({ label, value }: { label: string; value?: string | null }) {
  if (!value) return null;
  return (
    <div className="flex items-start gap-3 py-1 text-[12px]">
      <div className="w-24 shrink-0 text-mute">{label}</div>
      <div className="flex-1 break-all font-mono text-slate-200">{value}</div>
    </div>
  );
}

function CorrelationLink({
  label,
  param,
  value,
  anchorAt,
}: {
  label: string;
  param: "identity" | "run_id" | "request_id" | "template_key";
  value?: string | null;
  anchorAt?: string;
}) {
  if (!value) return null;
  // The window is CENTERED on the source event so it captures the event
  // itself + ±N minutes of related activity. Without anchor, the
  // Investigations page would default to "last N minutes from now" and
  // miss anything older than the window — the bug the operator hit on
  // first try.
  const params: Record<string, string> = {
    [param]: value,
    minutes: "60",
  };
  if (anchorAt) params.anchor_at = anchorAt;
  const search = new URLSearchParams(params);
  return (
    <div className="flex items-start gap-3 py-1 text-[12px]">
      <div className="w-24 shrink-0 text-mute">{label}</div>
      <div className="flex flex-1 items-center gap-2">
        <span className="break-all font-mono text-slate-200">{value}</span>
        <Link
          to={`/investigations?${search.toString()}`}
          className="shrink-0 rounded border border-border bg-panel2 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-accent hover:bg-accent/10"
        >
          Investigate
        </Link>
      </div>
    </div>
  );
}

export default function IssueDetailDrawer({
  issueId,
  onClose,
  onSelectIssue,
}: {
  issueId: number | null;
  onClose: () => void;
  onSelectIssue: (id: number) => void;
}) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["issue-detail", issueId],
    queryFn: () => api.issueDetail(issueId!),
    enabled: issueId !== null,
  });

  // Close on Escape.
  useEffect(() => {
    if (issueId === null) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [issueId, onClose]);

  if (issueId === null) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 sm:p-8">
      {/* backdrop */}
      <div
        onClick={onClose}
        className="absolute inset-0 bg-black/70 backdrop-blur-sm"
      />
      {/* modal */}
      <div className="relative flex max-h-full w-full max-w-4xl flex-col overflow-hidden rounded-2xl border border-border bg-bg shadow-2xl">
        {/* header */}
        <div className="flex items-start justify-between gap-3 border-b border-border bg-panel px-5 py-4">
          <div className="min-w-0 flex-1">
            <div className="mb-1 flex flex-wrap items-center gap-2">
              {data ? <SeverityChip severity={data.severity} /> : null}
              <span className="text-sm font-semibold text-white">
                {data?.rule ?? "Loading…"}
              </span>
              {data ? (
                <span className="text-[11px] text-mute">
                  · {data.container}
                </span>
              ) : null}
            </div>
            {data ? (
              <div className="font-mono text-[11px] text-mute">
                {data.at}
              </div>
            ) : null}
          </div>
          <button
            onClick={onClose}
            className="shrink-0 rounded p-1.5 text-mute hover:bg-panel2 hover:text-white"
            title="Close (Esc)"
          >
            <X size={18} />
          </button>
        </div>

        {/* body */}
        <div className="flex-1 overflow-y-auto px-5 py-5">
          {isLoading && (
            <div className="py-16 text-center text-sm text-mute">
              Loading investigation…
            </div>
          )}
          {error && (
            <div className="py-16 text-center text-sm text-bad">
              Failed to load: {(error as Error).message}
            </div>
          )}
          {data && <DetailBody data={data} onSelectIssue={onSelectIssue} />}
        </div>
      </div>
    </div>
  );
}

function DetailBody({
  data,
  onSelectIssue,
}: {
  data: IssueDetail;
  onSelectIssue: (id: number) => void;
}) {
  const hasParsed =
    !!data.parsed.elapsed_ms ||
    !!data.parsed.count ||
    !!(data.parsed.value && data.parsed.threshold) ||
    !!data.parsed.error_type ||
    !!data.parsed.error;

  return (
    <div className="space-y-4">
      <Section
        icon={<Target size={14} />}
        title="What happened — expected vs observed"
      >
        <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
          <div className="rounded-lg border border-good/30 bg-good/5 p-3">
            <div className="mb-1 text-[10px] uppercase tracking-wider text-good">
              Expected
            </div>
            <div className="text-[12.5px] leading-relaxed text-slate-200">
              {data.analysis.expected_behavior || (
                <span className="text-mute">
                  No playbook entry for this rule.
                </span>
              )}
            </div>
          </div>
          <div className="rounded-lg border border-bad/30 bg-bad/5 p-3">
            <div className="mb-1 text-[10px] uppercase tracking-wider text-bad">
              Observed deviation
            </div>
            <div className="text-[12.5px] leading-relaxed text-slate-200">
              {data.analysis.observed_deviation || (
                <span className="text-mute">Manual analysis required.</span>
              )}
            </div>
          </div>
          <div className="rounded-lg border border-accent/30 bg-accent/5 p-3">
            <div className="mb-1 flex items-center gap-1 text-[10px] uppercase tracking-wider text-accent">
              <Wrench size={11} />
              Suggested action
            </div>
            <div className="text-[12.5px] leading-relaxed text-slate-200">
              {data.analysis.suggested_action || (
                <span className="text-mute">
                  No standard action — investigate manually.
                </span>
              )}
            </div>
          </div>
        </div>
      </Section>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <Section icon={<MapPin size={14} />} title="Where">
          <KeyVal label="Container" value={data.where.container} />
          <KeyVal label="Service" value={data.where.service} />
          <KeyVal
            label="HTTP"
            value={
              data.where.method
                ? `${data.where.method} ${data.where.path ?? ""}`
                : data.where.path ?? null
            }
          />
          <CorrelationLink
            label="Request ID"
            param="request_id"
            value={data.where.request_id}
            anchorAt={data.at}
          />
          <CorrelationLink
            label="Run ID"
            param="run_id"
            value={data.where.run_id}
            anchorAt={data.at}
          />
          <CorrelationLink
            label="Identity"
            param="identity"
            value={data.where.identity}
            anchorAt={data.at}
          />
          <CorrelationLink
            label="Template"
            param="template_key"
            value={data.where.template_key}
            anchorAt={data.at}
          />
          <KeyVal label="Tool" value={data.where.tool} />
          <KeyVal label="Filename" value={data.where.filename} />
        </Section>

        {(hasParsed || true) && (
          <Section icon={<Activity size={14} />} title="Telemetry">
            <KeyVal label="At (UTC)" value={data.at} />
            <KeyVal label="Captured" value={data.seen_at} />
            <KeyVal label="Elapsed (ms)" value={data.parsed.elapsed_ms} />
            {data.parsed.count && (
              <KeyVal
                label="Count"
                value={`${data.parsed.count}${
                  data.parsed.window_seconds
                    ? ` in ${data.parsed.window_seconds}s`
                    : ""
                }`}
              />
            )}
            {data.parsed.value && data.parsed.threshold && (
              <KeyVal
                label="Value"
                value={`${data.parsed.value} (> ${data.parsed.threshold})`}
              />
            )}
            <KeyVal label="Error type" value={data.parsed.error_type} />
            <KeyVal label="Error" value={data.parsed.error} />
          </Section>
        )}
      </div>

      {data.rule_spec && (
        <Section
          icon={<AlertTriangle size={14} />}
          title="Rule that matched"
        >
          <div className="grid grid-cols-1 gap-x-6 md:grid-cols-2">
            <div>
              <KeyVal label="Rule" value={data.rule_spec.name} />
              <KeyVal label="Kind" value={data.rule_spec.kind} />
              <KeyVal label="Severity" value={data.rule_spec.severity} />
              {data.rule_spec.threshold !== undefined && (
                <KeyVal
                  label="Threshold"
                  value={String(data.rule_spec.threshold)}
                />
              )}
              {data.rule_spec.window_seconds !== undefined && (
                <KeyVal
                  label="Window (s)"
                  value={String(data.rule_spec.window_seconds)}
                />
              )}
            </div>
            <div>
              {data.rule_spec.description && (
                <KeyVal
                  label="Description"
                  value={data.rule_spec.description}
                />
              )}
              {data.rule_spec.pattern && (
                <KeyVal label="Pattern" value={data.rule_spec.pattern} />
              )}
              {data.rule_spec.exclude && (
                <KeyVal label="Exclude" value={data.rule_spec.exclude} />
              )}
            </div>
          </div>
        </Section>
      )}

      <Section icon={<FileText size={14} />} title="Raw log line">
        <pre className="overflow-x-auto whitespace-pre-wrap break-all rounded bg-panel2 p-3 text-[11px] leading-relaxed text-slate-200">
          {data.inner_line}
        </pre>
      </Section>

      {data.related.length > 0 && (
        <Section
          icon={<ListTree size={14} />}
          title={`Related events (±2 min · ${data.related.length})`}
        >
          <div className="rounded border border-border bg-panel2">
            {data.related.map((r: Issue) => (
              <button
                key={r.id}
                onClick={() => r.id && onSelectIssue(r.id)}
                className="flex w-full items-start gap-3 border-t border-border px-3 py-2 text-left text-[11px] first:border-t-0 hover:bg-panel"
              >
                <div className="w-20 shrink-0 font-mono text-mute">
                  {r.at.slice(11, 19)}
                </div>
                <SeverityChip severity={r.severity} />
                <div className="w-40 shrink-0 truncate text-slate-300">
                  {r.container}
                </div>
                <div className="min-w-0 flex-1 truncate text-white" title={r.rule}>
                  {r.rule}
                </div>
              </button>
            ))}
          </div>
        </Section>
      )}
    </div>
  );
}
