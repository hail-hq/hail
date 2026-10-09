import { Suspense } from "react";
import { llm, stt, tts } from "@/lib/costs";
import { CompareModels } from "@/components/compare-picker";
import { featuredPairs } from "@/lib/featured";
import { SITE_ORIGIN } from "@/lib/url";
import { pageMetadata } from "../../../../shared/site-metadata";

export const dynamic = "force-static";

export const metadata = pageMetadata({
  title: "Compare AI model costs | hail.so",
  description:
    "Compare pricing for language, speech recognition, and speech synthesis models. View model costs and capabilities side by side.",
  path: "/costs/compare",
  image: "/costs/opengraph-image",
  origin: SITE_ORIGIN,
});

export default function ComparePage() {
  return (
    <>
      <header
        style={{
          padding: "40px 0 28px",
          borderBottom: "2px solid var(--color-ink)",
        }}
      >
        <div className="wrap">
          <h1 className="dispatch-h1">Side by side.</h1>
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
              fontSize: 15,
              fontWeight: 600,
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
