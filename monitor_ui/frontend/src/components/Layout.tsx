import { NavLink, Outlet } from "react-router-dom";
import {
  Activity,
  AlertTriangle,
  Users,
  Settings as SettingsIcon,
  Search,
  Network,
  HeartPulse,
  Rocket,
  CheckCircle2,
  Brain,
  Cpu,
  ShieldHalf,
  Sun,
  Moon,
  Bell,
  RefreshCw,
} from "lucide-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { useTheme } from "../lib/theme";
import AegisLogo from "./AegisLogo";

// -- left rail ---------------------------------------------------------------

function NavItem({
  to,
  icon,
  label,
  badge,
}: {
  to: string;
  icon: JSX.Element;
  label: string;
  badge?: string;
}) {
  return (
    <NavLink
      to={to}
      end
      className={({ isActive }) =>
        `group flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition ${
          isActive
            ? "bg-accent/15 text-accent shadow-glowSoft"
            : "text-muted hover:bg-panel2 hover:text-ink"
        }`
      }
    >
      <span className="opacity-90 group-hover:opacity-100">{icon}</span>
      <span className="flex-1">{label}</span>
      {badge ? (
        <span className="rounded bg-panel2 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-muted">
          {badge}
        </span>
      ) : null}
    </NavLink>
  );
}

// Pulse-on-glow status chip at the bottom of the sidebar.
// NB: Tailwind JIT can't see dynamic class strings, so both branches use
// literal classnames.
function StatusPulse() {
  const { data } = useQuery({
    queryKey: ["connection"],
    queryFn: () => api.connection(),
    refetchInterval: 10000,
    refetchIntervalInBackground: true,
  });
  const ok = !!data?.ok;
  return (
    <div
      className={
        ok
          ? "flex items-center gap-3 rounded-xl border border-good/30 bg-good/10 px-3 py-2"
          : "flex items-center gap-3 rounded-xl border border-bad/30 bg-bad/10 px-3 py-2"
      }
    >
      <span className="relative flex h-2.5 w-2.5">
        <span
          className={
            ok
              ? "absolute inline-flex h-full w-full animate-pulseSoft rounded-full bg-good"
              : "absolute inline-flex h-full w-full animate-pulseSoft rounded-full bg-bad"
          }
        />
        <span
          className={
            ok
              ? "relative inline-flex h-2.5 w-2.5 rounded-full bg-good"
              : "relative inline-flex h-2.5 w-2.5 rounded-full bg-bad"
          }
        />
      </span>
      <div className="text-[10px] leading-tight">
        <div className="font-semibold uppercase tracking-[0.18em] text-muted">
          AEGIS STATUS
        </div>
        <div
          className={
            ok
              ? "text-[12px] font-semibold tracking-wider text-good"
              : "text-[12px] font-semibold tracking-wider text-bad"
          }
        >
          {ok ? "GUARDING" : "OFFLINE"}
        </div>
      </div>
    </div>
  );
}

// -- top action bar ----------------------------------------------------------

function TopBar() {
  const { theme, toggle } = useTheme();
  const qc = useQueryClient();
  // Reuse the same connection query the sidebar pulses on.
  const { data } = useQuery({
    queryKey: ["connection"],
    queryFn: () => api.connection(),
    refetchInterval: 10000,
    refetchIntervalInBackground: true,
  });

  const autonomous = "Observe-Only"; // placeholder — flips when self-heal lands.

  return (
    <div className="sticky top-0 z-30 flex h-14 items-center gap-2 border-b border-border bg-bg/85 px-6 backdrop-blur-sm">
      <div className="hidden flex-1 md:block">
        <div className="text-[11px] uppercase tracking-[0.2em] text-muted">
          Real-time system intelligence
        </div>
        <div className="text-xs text-muted/70">
          {data?.ok
            ? `Tunnel ${data.tunnel?.local_port ?? "—"} → ${
                data.tunnel?.remote ?? ""
              }`
            : "Tunnel offline — refresh will auto-reconnect"}
        </div>
      </div>

      <button
        onClick={() => qc.invalidateQueries()}
        title="Refresh all queries"
        className="rounded-lg border border-border bg-panel px-2.5 py-1.5 text-xs text-muted hover:bg-panel2 hover:text-ink"
      >
        <RefreshCw size={14} />
      </button>
      <button
        title="Notifications"
        className="rounded-lg border border-border bg-panel px-2.5 py-1.5 text-xs text-muted hover:bg-panel2 hover:text-ink"
      >
        <Bell size={14} />
      </button>

      <div className="flex items-center gap-2 rounded-lg border border-border bg-panel px-3 py-1.5">
        <span className="text-[10px] uppercase tracking-wider text-muted">
          Mode
        </span>
        <span className="text-xs font-semibold text-accent">{autonomous}</span>
      </div>

      <button
        onClick={toggle}
        title={`Switch to ${theme === "dark" ? "light" : "dark"}`}
        className="rounded-lg border border-border bg-panel px-2.5 py-1.5 text-muted hover:bg-panel2 hover:text-ink"
      >
        {theme === "dark" ? <Sun size={14} /> : <Moon size={14} />}
      </button>
    </div>
  );
}

// -- Layout shell ------------------------------------------------------------

export default function Layout() {
  return (
    <div className="flex h-screen bg-bg text-ink">
      <aside className="flex w-64 flex-col border-r border-border bg-panel">
        <div className="starfield relative border-b border-border px-5 py-5">
          <div className="flex items-center gap-3">
            <AegisLogo size={36} />
            <div>
              <div className="text-lg font-semibold tracking-wider text-ink">
                AEGIS
              </div>
              <div className="text-[9px] uppercase tracking-[0.18em] text-muted">
                Agent Ecosystem Guardian
              </div>
            </div>
          </div>
          <div className="mt-3 text-[10px] uppercase tracking-[0.22em] text-accent/85">
            Guard · Analyze · Heal · Evolve
          </div>
        </div>

        <nav className="flex-1 space-y-1 overflow-y-auto px-3 py-4">
          <div className="px-2 pb-1 text-[10px] uppercase tracking-[0.18em] text-muted">
            Live
          </div>
          <NavItem to="/" icon={<Activity size={16} />} label="Overview" />
          <NavItem
            to="/issues"
            icon={<AlertTriangle size={16} />}
            label="Issues"
          />
          <NavItem
            to="/investigations"
            icon={<Search size={16} />}
            label="Investigations"
          />

          <div className="px-2 pb-1 pt-4 text-[10px] uppercase tracking-[0.18em] text-muted">
            Self-healing
          </div>
          <NavItem
            to="/behavior-twin"
            icon={<Network size={16} />}
            label="Behavior Twin"
            badge="Preview"
          />
          <NavItem
            to="/root-cause"
            icon={<Brain size={16} />}
            label="Root Cause"
            badge="Preview"
          />
          <NavItem
            to="/self-heal"
            icon={<HeartPulse size={16} />}
            label="Self Heal"
            badge="Soon"
          />
          <NavItem
            to="/deployments"
            icon={<Rocket size={16} />}
            label="Deployments"
            badge="Soon"
          />
          <NavItem
            to="/verification"
            icon={<CheckCircle2 size={16} />}
            label="Verification"
            badge="Soon"
          />

          <div className="px-2 pb-1 pt-4 text-[10px] uppercase tracking-[0.18em] text-muted">
            Governance
          </div>
          <NavItem
            to="/knowledge-graph"
            icon={<Cpu size={16} />}
            label="Knowledge Graph"
            badge="Soon"
          />
          <NavItem
            to="/agents"
            icon={<ShieldHalf size={16} />}
            label="Agents"
            badge="Soon"
          />
          <NavItem
            to="/test-users"
            icon={<Users size={16} />}
            label="Test Users"
          />
          <NavItem
            to="/settings"
            icon={<SettingsIcon size={16} />}
            label="Settings"
          />
        </nav>

        <div className="border-t border-border p-3">
          <StatusPulse />
          <div className="mt-3 text-center text-[10px] uppercase tracking-[0.18em] text-muted">
            v0.2 · localhost only
          </div>
        </div>
      </aside>

      <main className="flex flex-1 flex-col overflow-hidden">
        <TopBar />
        <div className="flex-1 overflow-auto p-6">
          <Outlet />
        </div>
      </main>
    </div>
  );
}
