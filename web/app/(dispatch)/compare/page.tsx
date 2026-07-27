import { Suspense } from "react";
import { llm, stt, tts } from "@/lib/costs";
import { CompareModels } from "@/components/compare-picker";
import { featuredPairs } from "@/lib/featured";

export const dynamic = "force-static";

export const metadata = {
  title: "Compare model costs — Hail",
  description:
    "Compare AI model providers side-by-side. Schema-validated, refreshed weekly.",
};

export default function ComparePage() {
  const today = new Date().toISOString().slice(0, 10);

  return (
    <>
      <div className="dispatch-tape">
        <div className="wrap row">
          <div className="left">
            <span className="dot">●</span> HAIL.SO / DISPATCH · {today} ·
            COMPARE
          </div>
        </div>
      </div>

      <header
        style={{
          padding: "40px 0 28px",
          borderBottom: "2px solid var(--color-ink)",
        }}
      >
        <div className="wrap">
          <h1 className="dispatch-h1">
            <em className="it">side</em> by side.
          </h1>
        </div>
      </header>

      <Suspense fallback={null}>
        <CompareModels llm={llm.models} stt={stt.models} tts={tts.models} />
      </Suspense>

      <section
        style={{ padding: "32px 0", borderTop: "2px solid var(--color-ink)" }}
      >
        <div className="wrap">
          <h2
            style={{
              fontFamily: "var(--font-mono)",
              fontSize: 11,
              fontWeight: 700,
              letterSpacing: "0.14em",
              textTransform: "uppercase",
              color: "var(--color-mute)",
              margin: "0 0 16px",
            }}
          >
            Popular comparisons
          </h2>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            {featuredPairs.map((p) => (
              <a
                key={p.slug}
                className="add-pill"
                href={`/costs/compare/${p.slug}`}
              >
                {p.models[0].display_name} vs {p.models[1].display_name}
              </a>
            ))}
          </div>
        </div>
      </section>
    </>
  );
}
