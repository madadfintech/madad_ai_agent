import { NavLink, Outlet } from "react-router-dom";
import { Activity, AlertTriangle, Users, Settings } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";

function NavItem({ to, icon, label }: { to: string; icon: JSX.Element; label: string }) {
  return (
    <NavLink
      to={to}
      end
      className={({ isActive }) =>
        `flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition ${
          isActive
            ? "bg-panel2 text-white"
            : "text-mute hover:bg-panel2 hover:text-slate-100"
        }`
      }
    >
      {icon}
      <span>{label}</span>
    </NavLink>
  );
}

function StatusDot() {
  const { data } = useQuery({
    queryKey: ["connection"],
    queryFn: () => api.connection(),
    refetchInterval: 10000,
  });
  const ok = data?.ok;
  return (
    <span
      title={
        ok
          ? `tunnel open: ${data?.tunnel.local_port} → ${data?.tunnel.remote}`
          : data?.tunnel.error || "tunnel down"
      }
      className={`ml-auto inline-block h-2 w-2 rounded-full ${
        ok ? "bg-good" : "bg-bad"
      }`}
    />
  );
}

export default function Layout() {
  return (
    <div className="flex h-screen">
      <aside className="flex w-60 flex-col border-r border-border bg-panel p-4">
        <div className="mb-6 flex items-center">
          <span className="text-base font-semibold text-white">
            Madad Monitor
          </span>
          <StatusDot />
        </div>
        <nav className="space-y-1">
          <NavItem to="/" icon={<Activity size={18} />} label="Dashboard" />
          <NavItem
            to="/issues"
            icon={<AlertTriangle size={18} />}
            label="Issues"
          />
          <NavItem
            to="/test-users"
            icon={<Users size={18} />}
            label="Test Users"
          />
          <NavItem
            to="/settings"
            icon={<Settings size={18} />}
            label="Settings"
          />
        </nav>
        <div className="mt-auto pt-4 text-xs text-mute">
          v0.1.0 · localhost only
        </div>
      </aside>
      <main className="flex-1 overflow-auto p-6">
        <Outlet />
      </main>
    </div>
  );
}
