import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";
import {
  Activity,
  Target,
  Wrench,
  Gauge,
  Clock,
  AlertTriangle,
  HeartPulse,
  CheckCircle2,
  Layers,
} from "lucide-react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";

// -- shared bits -------------------------------------------------------------

function KpiCard({
  icon,
  label,
  value,
  hint,
  tone = "accent",
}: {
  icon: JSX.Element;
  label: string;
  value: string | number;
  hint?: string;
  tone?: "accent" | "accent2" | "good" | "warn" | "bad";
}) {
  const ring = {
    accent: "from-accent/30 to-transparent border-accent/30",
    accent2: "from-accent2/30 to-transparent border-accent2/30",
    good: "from-good/30 to-transparent border-good/30",
    warn: "from-warn/30 to-transparent border-warn/30",
    bad: "from-bad/30 to-transparent border-bad/30",
  }[tone];
  const halo = {
    accent: "bg-accent/15 text-accent",
    accent2: "bg-accent2/15 text-accent2",
    good: "bg-good/15 text-good",
    warn: "bg-warn/15 text-warn",
    bad: "bg-bad/15 text-bad",
  }[tone];

  return (
    <div
      className={`relative overflow-hidden rounded-2xl border bg-panel p-4 ${ring}`}
    >
      <div className={`absolute inset-x-0 top-0 h-px bg-gradient-to-r ${ring}`} />
      <div className="flex items-start gap-3">
        <div
          className={`flex h-10 w-10 items-center justify-center rounded-xl ${halo}`}
        >
          {icon}
        </div>
        <div className="flex-1">
          <div className="text-[10px] uppercase tracking-[0.16em] text-muted">
            {label}
          </div>
          <div className="mt-0.5 text-2xl font-semibold text-ink">{value}</div>
          {hint && <div className="text-[11px] text-muted">{hint}</div>}
        </div>
      </div>
    </div>
  );
}

function SeverityDot({ severity }: { severity: string }) {
  const cls =
    severity === "error"
      ? "bg-bad shadow-glowBad"
      : severity === "warning"
      ? "bg-warn shadow-glowWarn"
      : "bg-good shadow-glowGood";
  return <span className={`inline-block h-2 w-2 rounded-full ${cls}`} />;
}

// -- main page ---------------------------------------------------------------

export default function Dashboard() {
  const qc = useQueryClient();
  const { data: stats } = useQuery({
    queryKey: ["stats"],
    queryFn: api.stats,
    refetchInterval: 5000,
    refetchIntervalInBackground: true,
  });
  const { data: issues } = useQuery({
    queryKey: ["issues-recent"],
    queryFn: () => api.issues({ n: 25, source: "live" }),
    refetchInterval: 5000,
    refetchIntervalInBackground: true,
  });

  // Derived metrics.
  const totals = useMemo(() => {
    const t = stats?.total ?? 0;
    const errors = stats?.by_severity?.error ?? 0;
    const warns = stats?.by_severity?.warning ?? 0;
    const info = stats?.by_severity?.info ?? 0;
    return { t, errors, warns, info };
  }, [stats]);

  // Top deviation buckets.
  const topRules = useMemo(() => {
    const by = stats?.by_rule ?? {};
    return Object.entries(by)
      .filter(([k]) => k !== "dedupe_caught_duplicate")
      .sort((a, b) => b[1] - a[1])
      .slice(0, 4);
  }, [stats]);

  const dedupeHits = stats?.by_rule?.dedupe_caught_duplicate ?? 0;
  const recent = issues?.issues ?? [];

  return (
    <div className="space-y-5">
      {/* Section heading + actions */}
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-wide text-ink">
            Overview
          </h1>
          <p className="text-sm text-muted">
            Real-time system intelligence and autonomous operations
          </p>
        </div>
        <Link
          to="/issues"
          className="rounded-lg border border-border bg-panel px-3 py-1.5 text-xs text-muted hover:bg-panel2 hover:text-ink"
        >
          View all deviations →
        </Link>
      </div>

      {/* KPI strip */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
        <KpiCard
          icon={<Activity size={18} />}
          label="System health"
          tone="good"
          value={totals.errors === 0 ? "Healthy" : "Degraded"}
          hint={`${stats?.log_size_bytes ?? 0} B captured`}
        />
        <KpiCard
          icon={<Target size={18} />}
          label="Deviations"
          tone="accent"
          value={totals.t}
          hint={`${totals.errors} errors · ${totals.warns} warnings`}
        />
        <KpiCard
          icon={<Wrench size={18} />}
          label="Autonomous fixes"
          tone="accent2"
          value="0"
          hint="self-heal pending"
        />
        <KpiCard
          icon={<Gauge size={18} />}
          label="Success rate"
          tone="good"
          value={
            totals.t === 0
              ? "100%"
              : `${(((totals.t - totals.errors) / totals.t) * 100).toFixed(1)}%`
          }
          hint="non-error share"
        />
        <KpiCard
          icon={<Clock size={18} />}
          label="MTTR"
          tone="warn"
          value="—"
          hint="needs self-heal data"
        />
      </div>

      {/* Mid grid: deviations / autonomous loop */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        {/* Top deviations */}
        <div className="lg:col-span-2 rounded-2xl border border-border bg-panel p-4">
          <div className="mb-3 flex items-center justify-between">
            <div>
              <div className="flex items-center gap-2">
                <AlertTriangle size={14} className="text-warn" />
                <h2 className="text-sm font-semibold text-ink">
                  Deviations detected
                </h2>
              </div>
              <p className="text-[11px] text-muted">
                Top behavioural deviations across the last poll window
              </p>
            </div>
            <Link
              to="/issues"
              className="text-[11px] uppercase tracking-wider text-accent hover:underline"
            >
              All →
            </Link>
          </div>

          {topRules.length === 0 ? (
            <div className="py-8 text-center text-sm text-muted">
              No deviations in this window. The system is healthy. ✓
            </div>
          ) : (
            <div className="space-y-2">
              {topRules.map(([rule, count]) => {
                const tone =
                  rule.includes("terminal_failure") ||
                  rule.includes("5xx") ||
                  rule.includes("resubmit_loop")
                    ? "bad"
                    : rule.includes("slow") || rule.includes("duplicate")
                    ? "warn"
                    : "accent";
                const toneCls =
                  tone === "bad"
                    ? "border-bad/30 bg-bad/5"
                    : tone === "warn"
                    ? "border-warn/30 bg-warn/5"
                    : "border-accent/30 bg-accent/5";
                const badgeCls =
                  tone === "bad"
                    ? "bg-bad/20 text-bad"
                    : tone === "warn"
                    ? "bg-warn/20 text-warn"
                    : "bg-accent/20 text-accent";
                return (
                  <div
                    key={rule}
                    className={`flex items-center justify-between rounded-xl border px-3 py-2 ${toneCls}`}
                  >
                    <div className="flex items-center gap-3">
                      <SeverityDot
                        severity={tone === "bad" ? "error" : "warning"}
                      />
                      <div>
                        <div className="text-sm font-mono text-ink">{rule}</div>
                        <div className="text-[10px] uppercase tracking-wider text-muted">
                          rule
                        </div>
                      </div>
                    </div>
                    <span
                      className={`rounded px-2 py-0.5 text-[10px] uppercase tracking-wider ${badgeCls}`}
                    >
                      {count} hits
                    </span>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* Autonomous operation loop (placeholder until self-heal lands) */}
        <div className="rounded-2xl border border-border bg-panel p-4">
          <div className="mb-3 flex items-center gap-2">
            <HeartPulse size={14} className="text-accent" />
            <h2 className="text-sm font-semibold text-ink">
              Autonomous loop
            </h2>
          </div>
          <ul className="space-y-3 text-[12px]">
            <LoopStep
              ok
              title="Detect"
              note={`${totals.t} events captured this window`}
            />
            <LoopStep
              ok
              title="Investigate"
              note="Per-issue analysis online"
            />
            <LoopStep
              ok={false}
              title="Generate fix"
              note="Playbook ready · execution pending"
            />
            <LoopStep
              ok={false}
              title="Deploy"
              note="Self-heal target Q3 2026"
            />
            <LoopStep
              ok={false}
              title="Verify"
              note="Closes the loop"
            />
          </ul>
        </div>
      </div>

      {/* Recent activity table */}
      <div className="rounded-2xl border border-border bg-panel p-4">
        <div className="mb-3 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Layers size={14} className="text-accent" />
            <h2 className="text-sm font-semibold text-ink">Recent activity</h2>
          </div>
          <button
            onClick={() => qc.invalidateQueries()}
            className="rounded border border-border bg-panel2 px-2 py-1 text-[10px] uppercase tracking-wider text-muted hover:text-ink"
          >
            Refresh
          </button>
        </div>
        {recent.length === 0 ? (
          <div className="py-6 text-center text-sm text-muted">
            No recent events. Quiet system, healthy fleet.
          </div>
        ) : (
          <div className="overflow-hidden rounded-xl border border-border">
            <div className="grid grid-cols-12 border-b border-border bg-panel2 px-3 py-2 text-[10px] uppercase tracking-[0.16em] text-muted">
              <div className="col-span-2">Time</div>
              <div className="col-span-1">Sev</div>
              <div className="col-span-3">Container</div>
              <div className="col-span-3">Rule</div>
              <div className="col-span-3">Line</div>
            </div>
            <div className="max-h-[40vh] overflow-auto">
              {recent.slice(0, 12).map((r, i) => (
                <Link
                  key={r.id ?? i}
                  to={r.id ? `/issues?focus=${r.id}` : "/issues"}
                  className="grid grid-cols-12 items-center gap-1 border-t border-border px-3 py-2 font-mono text-[11px] hover:bg-panel2"
                >
                  <div className="col-span-2 text-muted">
                    {r.at.slice(11, 19)}
                  </div>
                  <div className="col-span-1">
                    <SeverityDot severity={r.severity} />
                  </div>
                  <div className="col-span-3 truncate text-muted">
                    {r.container}
                  </div>
                  <div className="col-span-3 truncate text-ink">{r.rule}</div>
                  <div className="col-span-3 truncate text-muted" title={r.line}>
                    {r.line}
                  </div>
                </Link>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Footer pill — sentinel pulse */}
      <div className="flex items-center justify-center gap-3 rounded-2xl border border-border bg-panel px-4 py-3 text-[11px] uppercase tracking-[0.18em] text-muted">
        <CheckCircle2 size={14} className="text-good" />
        AEGIS sentinel · {dedupeHits} dedupe catches confirmed ·
        {totals.t} total events
      </div>
    </div>
  );
}

function LoopStep({
  title,
  note,
  ok,
}: {
  title: string;
  note: string;
  ok: boolean;
}) {
  return (
    <li className="flex items-start gap-3">
      <span
        className={
          ok
            ? "mt-1 inline-flex h-2 w-2 rounded-full bg-good shadow-glowGood"
            : "mt-1 inline-flex h-2 w-2 rounded-full bg-muted/50"
        }
      />
      <div>
        <div className={ok ? "font-semibold text-ink" : "text-muted"}>
          {title}
        </div>
        <div className="text-[10px] uppercase tracking-wider text-muted">
          {note}
        </div>
      </div>
    </li>
  );
}
