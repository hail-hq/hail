import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { featuredPairs, pairBySlug, pairSlug } from "@/lib/featured";
import type { FeaturedPair } from "@/lib/featured";
import {
  LLMCompareTable,
  STTCompareTable,
  TTSCompareTable,
} from "@/components/compare-table";
import { DeprecationNotice } from "@/components/deprecation-notice";
import { SITE_ORIGIN } from "@/lib/url";
import type { LLMRow, STTRow, TTSRow } from "@/lib/types";

export const dynamic = "force-static";
export const dynamicParams = false;

export function generateStaticParams() {
  return featuredPairs.map((p) => ({ pair: p.slug }));
}

export async function generateMetadata({
  params,
}: {
  params: Promise<{ pair: string }>;
}): Promise<Metadata> {
  const { pair } = await params;
  const entry = pairBySlug.get(pair);
  if (!entry) return {};
  const [a, b] = entry.models;
  return {
    title: `${a.display_name} vs ${b.display_name} — cost comparison`,
    description: `Side-by-side pricing and capabilities for ${a.display_name} (${a.provider}) and ${b.display_name} (${b.provider}). Schema-validated, refreshed weekly.`,
    alternates: {
      canonical: new URL(`/costs/compare/${pair}`, SITE_ORIGIN).toString(),
    },
  };
}

function successorSlugFor(entry: FeaturedPair): string | null {
  const [a, b] = entry.models;
  const replacement = a.deprecated_at
    ? a.replaced_by_model_id
    : b.replaced_by_model_id;
  const survivor = a.deprecated_at ? b : a;
  if (!replacement) return null;
  const candidate = pairSlug(replacement, survivor.model_id);
  return pairBySlug.has(candidate) ? candidate : null;
}

export default async function PairPage({
  params,
}: {
  params: Promise<{ pair: string }>;
}) {
  const { pair } = await params;
  const entry = pairBySlug.get(pair);
  if (!entry) notFound();

  const [a, b] = entry.models;
  const currentIds = [a.model_id, b.model_id];
  const today = new Date().toISOString().slice(0, 10);

  return (
    <>
      <div className="dispatch-tape">
        <div className="wrap row">
          <div className="left">
            <span className="dot">●</span> HAIL.SO / DISPATCH · {today} ·
            COMPARE
          </div>
          <div className="right">
            FILE: <b>{entry.category.toUpperCase()}</b> · 2 models
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
            {a.display_name} <em className="it">vs</em> {b.display_name}
          </h1>
        </div>
      </header>

      <div className="toolbar">
        <div className="wrap row">
          <a href="/costs" className="btn btn-outline">
            ← all costs
          </a>
          <a href="/costs/compare" className="btn btn-outline">
            build your own
          </a>
        </div>
      </div>

      <section className="cat">
        <div className="wrap">
          <DeprecationNotice
            models={entry.models}
            successorSlug={successorSlugFor(entry)}
          />
          {entry.category === "llm" && (
            <LLMCompareTable
              models={entry.models as LLMRow[]}
              currentIds={currentIds}
              removable={false}
            />
          )}
          {entry.category === "stt" && (
            <STTCompareTable
              models={entry.models as STTRow[]}
              currentIds={currentIds}
              removable={false}
            />
          )}
          {entry.category === "tts" && (
            <TTSCompareTable
              models={entry.models as TTSRow[]}
              currentIds={currentIds}
              removable={false}
            />
          )}
        </div>
      </section>
    </>
  );
}
