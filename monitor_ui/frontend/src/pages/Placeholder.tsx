import { Sparkles } from "lucide-react";

/**
 * Generic "this surface is on the AEGIS roadmap" placeholder. Used for
 * Behavior Twin / Root Cause / Self Heal / Deployments / Verification /
 * Knowledge Graph / Agents — surfaces in the nav so the navigation feels
 * complete, but explicit about being unbuilt instead of pretending.
 */
export default function Placeholder({
  title,
  tagline,
  bullets,
}: {
  title: string;
  tagline: string;
  bullets: string[];
}) {
  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <div>
        <h1 className="text-2xl font-semibold text-ink">{title}</h1>
        <p className="text-sm text-muted">{tagline}</p>
      </div>

      <div className="rounded-2xl border border-border bg-panel p-6">
        <div className="mb-4 flex items-center gap-2 text-accent">
          <Sparkles size={16} />
          <span className="text-[11px] uppercase tracking-[0.18em]">
            On the AEGIS roadmap
          </span>
        </div>
        <p className="text-sm text-ink">
          This surface is part of the self-healing trajectory. The
          observability + investigation layers are live today; the
          autonomous closure of the loop follows the order below.
        </p>
        <ul className="mt-4 space-y-2 text-sm">
          {bullets.map((b, i) => (
            <li key={i} className="flex items-start gap-3">
              <span className="mt-1.5 inline-block h-1.5 w-1.5 rounded-full bg-accent shadow-glowSoft" />
              <span className="text-muted">{b}</span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
