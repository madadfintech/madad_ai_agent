/** @type {import('tailwindcss').Config} */
//
// AEGIS — "Sentinel Dark" / "Sentinel Light"
// Colors are wired through CSS custom properties (see src/index.css) so a
// single class on <html> swaps the entire palette. Tokens here are semantic
// (panel / panel2 / accent / success / danger …), not literal hex values —
// every component reads them and inherits the active theme automatically.

const tokenColor = (varName) => `rgb(var(${varName}) / <alpha-value>)`;

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        // Surface scale (light → dark elevations).
        bg:       tokenColor("--c-bg"),
        panel:    tokenColor("--c-panel"),
        panel2:   tokenColor("--c-panel2"),
        elevated: tokenColor("--c-elevated"),
        border:   tokenColor("--c-border"),

        // Text scale.
        ink:    tokenColor("--c-ink"),
        muted:  tokenColor("--c-muted"),
        // Back-compat alias so older components don't break.
        mute:   tokenColor("--c-muted"),

        // Status / accent palette — matches the mockup spec exactly.
        accent:    tokenColor("--c-accent"),     // primary cyan
        accent2:   tokenColor("--c-accent2"),    // secondary purple
        good:      tokenColor("--c-good"),       // success green
        warn:      tokenColor("--c-warn"),       // warning amber
        bad:       tokenColor("--c-bad"),        // danger red
      },
      boxShadow: {
        glow:       "0 0 24px rgb(var(--c-accent) / 0.35)",
        glowSoft:   "0 0 12px rgb(var(--c-accent) / 0.25)",
        glowGood:   "0 0 16px rgb(var(--c-good) / 0.45)",
        glowWarn:   "0 0 16px rgb(var(--c-warn) / 0.40)",
        glowBad:    "0 0 16px rgb(var(--c-bad) / 0.40)",
      },
      fontFamily: {
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Monaco", "monospace"],
      },
      keyframes: {
        pulseSoft: {
          "0%, 100%": { opacity: "0.85" },
          "50%": { opacity: "0.35" },
        },
      },
      animation: {
        pulseSoft: "pulseSoft 2.2s ease-in-out infinite",
      },
    },
  },
  plugins: [],
};
