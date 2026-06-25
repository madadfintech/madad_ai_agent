/**
 * AEGIS shield — pure SVG so it inherits ``currentColor`` from the parent
 * and scales crisply. The geometric arrow inside the shield reads as
 * both an "A" (for AEGIS) and an arrow pointing UP (forward / protect).
 *
 * Pass ``size`` for the bounding square in px. The two cyan strokes use
 * the theme accent token so the logo recolors automatically in light
 * mode.
 */
export default function AegisLogo({
  size = 28,
  glow = true,
  className = "",
}: {
  size?: number;
  glow?: boolean;
  className?: string;
}) {
  const accent = "rgb(var(--c-accent))";
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
      style={
        glow
          ? { filter: "drop-shadow(0 0 6px rgb(var(--c-accent) / 0.6))" }
          : undefined
      }
    >
      <defs>
        <linearGradient id="aegisShield" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="rgb(var(--c-panel2))" />
          <stop offset="1" stopColor="rgb(var(--c-bg))" />
        </linearGradient>
        <linearGradient id="aegisHighlight" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0" stopColor="rgb(var(--c-border))" />
          <stop offset="0.5" stopColor="rgb(var(--c-ink) / 0.55)" />
          <stop offset="1" stopColor="rgb(var(--c-border))" />
        </linearGradient>
      </defs>

      {/* Outer chrome bevel. */}
      <path
        d="M32 2 L58 12 V32 C58 47 47 58 32 62 C17 58 6 47 6 32 V12 Z"
        fill="url(#aegisHighlight)"
      />
      {/* Inner shield fill. */}
      <path
        d="M32 6 L54 14 V32 C54 45 44 54 32 58 C20 54 10 45 10 32 V14 Z"
        fill="url(#aegisShield)"
        stroke={accent}
        strokeWidth="1.2"
      />

      {/* Central arrow / stylised A. */}
      <g>
        <path
          d="M32 16 L42 38 L32 34 L22 38 Z"
          fill="rgb(var(--c-panel2))"
          stroke={accent}
          strokeWidth="1.4"
          strokeLinejoin="round"
        />
        {/* Spine — bright glowing core. */}
        <line
          x1="32"
          y1="22"
          x2="32"
          y2="48"
          stroke={accent}
          strokeWidth="2.2"
          strokeLinecap="round"
        />
        {/* Twin pips on either side of the spine — the sentinel "eyes". */}
        <circle cx="26" cy="44" r="1.6" fill={accent} />
        <circle cx="38" cy="44" r="1.6" fill={accent} />
      </g>
    </svg>
  );
}
