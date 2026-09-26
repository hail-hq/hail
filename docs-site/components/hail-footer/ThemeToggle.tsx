"use client";

import { useEffect, useSyncExternalStore } from "react";

type Theme = "system" | "light" | "dark";

export function ThemeToggle() {
  const theme = useSyncExternalStore(subscribe, getTheme, () => "system");

  useEffect(() => {
    if (theme === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = theme;
  }, [theme]);

  function select(next: Theme) {
    if (next === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = next;
    // Storage is best-effort: Safari with cookies blocked and some embedded
    // webviews throw on access. The choice still applies to this document.
    try {
      if (next === "system") localStorage.removeItem(THEME_KEY);
      else localStorage.setItem(THEME_KEY, next);
    } catch {
      /* no persistence available */
    }
    window.dispatchEvent(new Event("hail-theme-change"));
  }

  return <div><b>theme</b><div className={styles} role="group" aria-label="Color theme"><button type="button" aria-pressed={theme === "system"} onClick={() => select("system")}>system</button><span>·</span><button type="button" aria-pressed={theme === "light"} onClick={() => select("light")}>light</button><span>·</span><button type="button" aria-pressed={theme === "dark"} onClick={() => select("dark")}>dark</button></div></div>;
}

const styles = "theme-toggle";

/** Mirrored by the pre-paint script in app/layout.tsx. Keep the two in sync. */
const THEME_KEY = "hail-theme";

/**
 * Runs during render as the useSyncExternalStore snapshot, so it must never
 * throw: a storage-access exception here would take the whole page down, not
 * just the toggle.
 */
function getTheme(): Theme {
  try {
    const saved = localStorage.getItem(THEME_KEY);
    return saved === "light" || saved === "dark" ? saved : "system";
  } catch {
    return "system";
  }
}

function subscribe(onChange: () => void) {
  window.addEventListener("hail-theme-change", onChange);
  window.addEventListener("storage", onChange);
  return () => {
    window.removeEventListener("hail-theme-change", onChange);
    window.removeEventListener("storage", onChange);
  };
}
