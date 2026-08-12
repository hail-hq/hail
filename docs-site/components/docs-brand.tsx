import type { ReactNode } from "react";

export function DocsBrand() {
  return (
    <span className="docs-brand" aria-label="hail.so docs">
      <strong>hail.so</strong>
      <span aria-hidden="true">/</span>
      <span>docs</span>
    </span>
  );
}

export function DocsCta({ children }: { children: ReactNode }) {
  return (
    <span className="docs-cta">
      <span>{children}</span>
      <span className="docs-cta__mark" aria-hidden="true">
        <span className="docs-cta__chevron">›</span>
        <span className="docs-cta__arrow">→</span>
      </span>
    </span>
  );
}
