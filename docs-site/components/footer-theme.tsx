"use client";

import { useTheme } from "fumadocs-ui/provider/base";
import { useSyncExternalStore } from "react";

const subscribe = () => () => {};
const client = () => true;
const server = () => false;

export function FooterTheme() {
  const { theme, setTheme } = useTheme();
  const mounted = useSyncExternalStore(subscribe, client, server);
  return (
    <div>
      <b>theme</b>
      <div className="theme-toggle">
        {(["system", "light", "dark"] as const).map((value, i) => (
          <span key={value}>
            {i > 0 ? "· " : ""}
            <button
              aria-pressed={(mounted ? theme : "system") === value}
              onClick={() => setTheme(value)}
            >
              {value}
            </button>
          </span>
        ))}
      </div>
    </div>
  );
}
