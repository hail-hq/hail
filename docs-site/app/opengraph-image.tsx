import { ImageResponse } from "next/og";
import { DOCS_SITE_COPY } from "@/lib/site-copy";

export const alt = "Hail docs. Phone, SMS, and email for AI agents.";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

export default function OpenGraphImage() {
  return new ImageResponse(
    <div
      style={{
        width: "100%",
        height: "100%",
        display: "flex",
        flexDirection: "column",
        justifyContent: "space-between",
        background: "#f4f1e9",
        color: "#0a0a0a",
        padding: "64px 72px",
        fontFamily: "monospace",
        borderTop: "12px solid #1f6fff",
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          fontSize: 28,
        }}
      >
        <span style={{ fontWeight: 700 }}>hail.so / docs</span>
        <span style={{ color: "#1f6fff" }}>● documentation</span>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>
        <div style={{ display: "flex", fontSize: 26, color: "#1f6fff" }}>
          ● {DOCS_SITE_COPY.eyebrow}
        </div>
        <div
          style={{
            display: "flex",
            maxWidth: 1000,
            fontSize: 76,
            lineHeight: 1.05,
            letterSpacing: "-4px",
          }}
        >
          build every channel your agent needs.
        </div>
      </div>

      <div style={{ display: "flex", color: "#64615b", fontSize: 24 }}>
        mcp / api / cli / self-host
      </div>
    </div>,
    size,
  );
}
