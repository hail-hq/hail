import { Suspense } from "react";
import { llm, stt, tts } from "@/lib/costs";
import { CompareModels } from "@/components/compare-picker";
import { featuredPairs } from "@/lib/featured";
import { SITE_ORIGIN } from "@/lib/url";
import { pageMetadata } from "../../../../shared/site-metadata";
import { JsonLd } from "../../../../shared/JsonLd";
import { collectionSchema } from "../../../../shared/structured-data";

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
      <JsonLd
        data={collectionSchema({
          title: "Compare AI model costs",
          description:
            "Compare pricing for language, speech recognition, and speech synthesis models.",
          path: "/costs/compare",
          origin: SITE_ORIGIN,
          items: featuredPairs.map((pair) => ({
            name: pair.models.map((model) => model.display_name).join(" vs "),
            path: `/costs/compare/${pair.slug}`,
          })),
        })}
      />
      <header className="costs-hero costs-hero--compact">
        <div className="wrap">
          <h1 className="dispatch-h1">Side by side.</h1>
        </div>
      </header>

      <Suspense fallback={null}>
        <CompareModels llm={llm.models} stt={stt.models} tts={tts.models} />
      </Suspense>

      <section className="costs-popular">
        <div className="wrap">
          <h2>Popular comparisons</h2>
          <div className="costs-model-options">
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
