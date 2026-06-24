import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { X, AlertTriangle, Activity, Wrench, Target, MapPin } from "lucide-react";
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
  children,
}: {
  icon: JSX.Element;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="border-t border-border px-4 py-4 first:border-t-0">
      <div className="mb-2 flex items-center gap-2 text-[11px] uppercase tracking-wider text-mute">
        {icon}
        <span>{title}</span>
      </div>
      {children}
    </section>
  );
}

function KeyVal({ label, value }: { label: string; value?: string | null }) {
  if (!value) return null;
  return (
    <div className="grid grid-cols-12 gap-3 py-1 text-[12px]">
      <div className="col-span-3 text-mute">{label}</div>
      <div className="col-span-9 font-mono text-slate-200 break-all">
        {value}
      </div>
    </div>
  );
}

function CorrelationLink({
  label,
  param,
  value,
}: {
  label: string;
  param: "identity" | "run_id" | "request_id" | "template_key";
  value?: string | null;
}) {
  if (!value) return null;
  const search = new URLSearchParams({ [param]: value, minutes: "60" });
  return (
    <div className="grid grid-cols-12 gap-3 py-1 text-[12px]">
      <div className="col-span-3 text-mute">{label}</div>
      <div className="col-span-9 font-mono text-slate-200 break-all">
        <span>{value}</span>
        <Link
          to={`/investigations?${search.toString()}`}
          className="ml-2 inline-block rounded border border-border bg-panel2 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-accent hover:bg-accent/10"
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

  if (issueId === null) return null;

  return (
    <div className="fixed inset-0 z-50 flex">
      {/* backdrop */}
      <div
        onClick={onClose}
        className="flex-1 bg-black/60 backdrop-blur-sm"
      />
      {/* drawer */}
      <aside className="w-[min(720px,95vw)] overflow-y-auto border-l border-border bg-bg shadow-2xl">
        <div className="sticky top-0 z-10 flex items-center justify-between border-b border-border bg-panel px-4 py-3">
          <div className="flex items-center gap-2">
            {data ? <SeverityChip severity={data.severity} /> : null}
            <span className="text-sm font-semibold text-white">
              {data?.rule ?? "Loading…"}
            </span>
          </div>
          <button
            onClick={onClose}
            className="rounded p-1 text-mute hover:bg-panel2 hover:text-white"
            title="Close"
          >
            <X size={18} />
          </button>
        </div>

        {isLoading && (
          <div className="p-8 text-center text-sm text-mute">
            Loading investigation…
          </div>
        )}
        {error && (
          <div className="p-8 text-center text-sm text-bad">
            Failed to load: {(error as Error).message}
          </div>
        )}

        {data && <DetailBody data={data} onSelectIssue={onSelectIssue} />}
      </aside>
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
  return (
    <div>
      <Section
        icon={<Target size={12} />}
        title="What happened — expected vs observed"
      >
        <div className="space-y-3 text-[13px] leading-relaxed">
          <div>
            <div className="text-[10px] uppercase tracking-wider text-good">
              Expected
            </div>
            <div className="text-slate-200">
              {data.analysis.expected_behavior || (
                <span className="text-mute">No playbook entry for this rule.</span>
              )}
            </div>
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wider text-bad">
              Observed deviation
            </div>
            <div className="text-slate-200">
              {data.analysis.observed_deviation || (
                <span className="text-mute">Manual analysis required.</span>
              )}
            </div>
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wider text-accent">
              <Wrench size={11} className="mr-1 inline" />
              Suggested action
            </div>
            <div className="text-slate-200">
              {data.analysis.suggested_action || (
                <span className="text-mute">No standard action — investigate manually.</span>
              )}
            </div>
          </div>
        </div>
      </Section>

      <Section icon={<MapPin size={12} />} title="Where">
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
        />
        <CorrelationLink
          label="Run ID"
          param="run_id"
          value={data.where.run_id}
        />
        <CorrelationLink
          label="Identity"
          param="identity"
          value={data.where.identity}
        />
        <CorrelationLink
          label="Template"
          param="template_key"
          value={data.where.template_key}
        />
        <KeyVal label="Tool" value={data.where.tool} />
        <KeyVal label="Filename" value={data.where.filename} />
      </Section>

      <Section icon={<Activity size={12} />} title="Telemetry">
        <KeyVal label="At (UTC)" value={data.at} />
        <KeyVal label="Captured" value={data.seen_at} />
        {data.parsed.elapsed_ms && (
          <KeyVal label="Elapsed (ms)" value={data.parsed.elapsed_ms} />
        )}
        {data.parsed.count && (
          <KeyVal
            label="Count in window"
            value={`${data.parsed.count}${
              data.parsed.window_seconds
                ? ` over ${data.parsed.window_seconds}s`
                : ""
            }`}
          />
        )}
        {data.parsed.value && data.parsed.threshold && (
          <KeyVal
            label="Value vs threshold"
            value={`${data.parsed.value} (> ${data.parsed.threshold})`}
          />
        )}
        {data.parsed.error_type && (
          <KeyVal label="Error type" value={data.parsed.error_type} />
        )}
        {data.parsed.error && (
          <KeyVal label="Error" value={data.parsed.error} />
        )}
      </Section>

      {data.rule_spec && (
        <Section icon={<AlertTriangle size={12} />} title="Rule that matched">
          <KeyVal label="Rule" value={data.rule_spec.name} />
          <KeyVal label="Kind" value={data.rule_spec.kind} />
          <KeyVal label="Severity" value={data.rule_spec.severity} />
          {data.rule_spec.description && (
            <KeyVal label="Description" value={data.rule_spec.description} />
          )}
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
          {data.rule_spec.pattern && (
            <KeyVal label="Pattern" value={data.rule_spec.pattern} />
          )}
          {data.rule_spec.exclude && (
            <KeyVal label="Exclude" value={data.rule_spec.exclude} />
          )}
        </Section>
      )}

      <Section icon={<Activity size={12} />} title="Raw log line">
        <pre className="overflow-x-auto rounded bg-panel p-3 text-[11px] leading-relaxed text-slate-200">
          {data.inner_line}
        </pre>
      </Section>

      {data.related.length > 0 && (
        <Section
          icon={<Activity size={12} />}
          title={`Related events (±2 min, ${data.related.length})`}
        >
          <div className="rounded border border-border bg-panel">
            {data.related.map((r: Issue) => (
              <button
                key={r.id}
                onClick={() => r.id && onSelectIssue(r.id)}
                className="grid w-full grid-cols-12 items-start gap-2 border-t border-border px-3 py-2 text-left text-[11px] first:border-t-0 hover:bg-panel2"
              >
                <div className="col-span-3 text-mute">{r.at.slice(11, 19)}</div>
                <div className="col-span-1">
                  <SeverityChip severity={r.severity} />
                </div>
                <div className="col-span-3 text-slate-300">{r.container}</div>
                <div className="col-span-5 truncate text-white" title={r.rule}>
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
