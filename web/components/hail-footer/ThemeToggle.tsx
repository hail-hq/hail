"use client";

import { useEffect, useSyncExternalStore } from "react";

/**
 * The switch's words. English by default. Other languages pass their copy (THEME_COPY in lib/i18n/chrome-copy.ts).
 * scripts/sync-footer.mjs copies this file as is to the hail repo, so it imports nothing from the site.
 */
export type ThemeCopy = {
  heading: string;
  group: string;
  system: string;
  light: string;
  dark: string;
  switchToLight: string;
  switchToDark: string;
};

const ENGLISH: ThemeCopy = {
  heading: "theme",
  group: "Color theme",
  system: "system",
  light: "light",
  dark: "dark",
  switchToLight: "Switch to light theme",
  switchToDark: "Switch to dark theme",
};

type Theme = "system" | "light" | "dark";

/** The saved choice: system (default), light, or dark. Shared by every control. */
export function useTheme(): Theme {
  return useSyncExternalStore(subscribe, getTheme, () => "system");
}

/** Apply and persist a choice. Every mounted control re-renders through the store. */
export function selectTheme(next: Theme) {
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

export function ThemeToggle({ copy: t = ENGLISH }: { copy?: ThemeCopy } = {}) {
  const theme = useTheme();

  useEffect(() => {
    if (theme === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = theme;
  }, [theme]);

  const select = selectTheme;

  return <div><b>{t.heading}</b><div className={styles} role="group" aria-label={t.group}><button type="button" aria-pressed={theme === "system"} onClick={() => select("system")}>{t.system}</button><button type="button" aria-pressed={theme === "light"} onClick={() => select("light")}>{t.light}</button><button type="button" aria-pressed={theme === "dark"} onClick={() => select("dark")}>{t.dark}</button></div></div>;
}

/**
 * One small button for the header: shows the theme in effect (sun for light,
 * moon for dark) and flips to the other one. "System" stays the default; the
 * button only ever writes light or dark, and the footer's three-way control
 * offers the way back to system. Before hydration the effective theme is
 * unknown, so the server render shows a neutral half-disc.
 */
export function ThemeButton({ className, copy: t = ENGLISH }: { className?: string; copy?: ThemeCopy }) {
  const theme = useTheme();
  const systemDark = useSyncExternalStore(subscribeSystem, getSystemDark, () => null);
  const effective: "light" | "dark" | null =
    theme === "system" ? (systemDark === null ? null : systemDark ? "dark" : "light") : theme;
  const next = effective === "dark" ? "light" : "dark";
  const label = next === "light" ? t.switchToLight : t.switchToDark;
  return (
    <button
      type="button"
      className={className}
      onClick={() => selectTheme(next)}
      aria-label={label}
      title={label}
    >
      <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" aria-hidden="true">
        {effective === "light" && (
          <>
            <circle cx="10" cy="10" r="3.6" />
            <path d="M10 2.5v2M10 15.5v2M2.5 10h2M15.5 10h2M4.7 4.7l1.4 1.4M13.9 13.9l1.4 1.4M4.7 15.3l1.4-1.4M13.9 6.1l1.4-1.4" />
          </>
        )}
        {effective === "dark" && <path d="M15.5 12.4A6.2 6.2 0 0 1 7.6 4.5a6.2 6.2 0 1 0 7.9 7.9Z" />}
        {effective === null && (
          <>
            <circle cx="10" cy="10" r="6.5" />
            <path d="M10 3.5v13A6.5 6.5 0 0 0 10 3.5Z" fill="currentColor" stroke="none" />
          </>
        )}
      </svg>
    </button>
  );
}

function getSystemDark(): boolean | null {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return false;
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

function subscribeSystem(onChange: () => void) {
  if (typeof window.matchMedia !== "function") return () => {};
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  mq.addEventListener("change", onChange);
  return () => mq.removeEventListener("change", onChange);
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
