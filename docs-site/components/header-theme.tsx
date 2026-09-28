"use client";

import { useTheme } from "fumadocs-ui/provider/base";
import { useSyncExternalStore } from "react";

const subscribe = () => () => {};
const client = () => true;
const server = () => false;

/** Use Fumadocs' store so header, sidebar, and footer always share a theme. */
export function HeaderTheme() {
  const { resolvedTheme, setTheme } = useTheme();
  const mounted = useSyncExternalStore(subscribe, client, server);
  const effective = mounted ? resolvedTheme : undefined;
  const next = effective === "dark" ? "light" : "dark";
  return (
    <button
      type="button"
      aria-label={`Switch to ${next} theme`}
      title={`Switch to ${next} theme`}
      onClick={() => setTheme(next)}
    >
      <svg
        width="18"
        height="18"
        viewBox="0 0 20 20"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        aria-hidden="true"
      >
        {effective === "light" ? (
          <>
            <circle cx="10" cy="10" r="3.6" />
            <path d="M10 2.5v2M10 15.5v2M2.5 10h2M15.5 10h2M4.7 4.7l1.4 1.4M13.9 13.9l1.4 1.4M4.7 15.3l1.4-1.4M13.9 6.1l1.4-1.4" />
          </>
        ) : effective === "dark" ? (
          <path d="M15.5 12.4A6.2 6.2 0 0 1 7.6 4.5a6.2 6.2 0 1 0 7.9 7.9Z" />
        ) : (
          <>
            <circle cx="10" cy="10" r="6.5" />
            <path
              d="M10 3.5v13A6.5 6.5 0 0 0 10 3.5Z"
              fill="currentColor"
              stroke="none"
            />
          </>
        )}
      </svg>
    </button>
  );
}
