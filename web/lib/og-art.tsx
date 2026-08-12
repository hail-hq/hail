import { COSTS_HERO_COPY } from "./site-copy";

const INK = "#111111";
const BG = "#f5f3ed";
const ACCENT = "#1665ff";
const MUTE = "#686868";

export const OG_SIZE = { width: 1200, height: 630 };

/** Mirrors hail.so's own opengraph-image.tsx layout (accent square + wordmark,
 * headline, tagline/domain footer) and palette, so /costs OG cards read as
 * the same site rather than a neighbouring property. The default Satori
 * sans is acceptable; do not fetch remote fonts here. */
export function OgArt() {
  return (
    <div
      style={{
        width: "100%",
        height: "100%",
        display: "flex",
        flexDirection: "column",
        justifyContent: "space-between",
        background: BG,
        color: INK,
        padding: 72,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
        <div style={{ fontSize: 30, fontWeight: 700 }}>hail.so / database</div>
        <div style={{ color: ACCENT, fontSize: 22 }}>
          {COSTS_HERO_COPY.badge}
        </div>
      </div>
      <div
        style={{
          fontSize: 74,
          fontWeight: 700,
          lineHeight: 1.04,
          letterSpacing: -3,
          maxWidth: 1000,
        }}
      >
        {COSTS_HERO_COPY.heading}
      </div>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          fontSize: 26,
        }}
      >
        <div style={{ color: ACCENT }}>llm / stt / tts / sms / telephony</div>
        <div style={{ color: MUTE }}>hail.so/costs</div>
      </div>
    </div>
  );
}
