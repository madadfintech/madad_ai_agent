import { useEffect, useState } from "react";

export type ThemeMode = "dark" | "light";

const STORAGE_KEY = "aegis.theme";

function readInitial(): ThemeMode {
  // SSR-safety, even though Vite dev only runs in the browser.
  if (typeof window === "undefined") return "dark";
  const stored = window.localStorage.getItem(STORAGE_KEY);
  if (stored === "dark" || stored === "light") return stored;
  // Fall back to the OS preference. AEGIS' aesthetic peaks in dark, so
  // an OS preference of "no-preference" → dark.
  const mql = window.matchMedia?.("(prefers-color-scheme: light)");
  return mql && mql.matches ? "light" : "dark";
}

function applyTheme(mode: ThemeMode) {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  root.classList.remove("light", "dark");
  root.classList.add(mode);
}

/**
 * Toggle dark/light AEGIS theme with persistence in localStorage.
 * Reads OS preference on first load when no choice is stored.
 */
export function useTheme(): {
  theme: ThemeMode;
  setTheme: (m: ThemeMode) => void;
  toggle: () => void;
} {
  const [theme, setThemeState] = useState<ThemeMode>(readInitial);

  // Sync the <html> class whenever theme changes. Done as an effect so it
  // also fires on first render (initial state from readInitial may differ
  // from the SSR-side class that was hardcoded in index.html).
  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  function setTheme(mode: ThemeMode) {
    if (typeof window !== "undefined") {
      window.localStorage.setItem(STORAGE_KEY, mode);
    }
    setThemeState(mode);
  }

  function toggle() {
    setTheme(theme === "dark" ? "light" : "dark");
  }

  return { theme, setTheme, toggle };
}
