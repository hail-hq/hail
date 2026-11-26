import { a2p10dlc, llm, numbers, onSale, sms, stt, tts } from "@/lib/costs";
import { LLMSection } from "@/components/categories/llm-section";
import { STTSection } from "@/components/categories/stt-section";
import { TTSSection } from "@/components/categories/tts-section";
import { SmsSection } from "@/components/categories/sms-section";
import { TelephonySection } from "@/components/categories/telephony-section";
import { Toolbar } from "@/components/toolbar";
import { mostRecent, priceRange, rowRange } from "@/lib/format";
import {
  COSTS_HERO_COPY,
  COSTS_SITE_DESCRIPTION,
  COSTS_SITE_TITLE,
} from "@/lib/site-copy";
import { SITE_ORIGIN } from "@/lib/url";
import { JsonLd } from "../../../shared/JsonLd";
import { collectionSchema } from "../../../shared/structured-data";

export const dynamic = "force-static";

export const metadata = {
  title: COSTS_SITE_TITLE,
  description: COSTS_SITE_DESCRIPTION,
  alternates: {
    canonical: "/costs",
    types: {
      "text/markdown": "/costs.md",
    },
  },
  openGraph: {
    type: "website",
    url: "/costs",
    siteName: "Hail",
    title: COSTS_SITE_TITLE,
    description: COSTS_SITE_DESCRIPTION,
    images: [
      {
        url: "/costs/opengraph-image",
        width: 1200,
        height: 630,
        alt: COSTS_HERO_COPY.heading,
      },
    ],
  },
  twitter: {
    card: "summary_large_image",
    site: "@hail_hq",
    creator: "@hail_hq",
    title: COSTS_SITE_TITLE,
    description: COSTS_SITE_DESCRIPTION,
    images: ["/costs/opengraph-image"],
  },
};

export default function CostsPage() {
  const today = new Date().toISOString().slice(0, 10);

  const totalModels = llm.models.length + stt.models.length + tts.models.length;
  const providers = new Set<string>();
  for (const m of [...llm.models, ...stt.models, ...tts.models]) {
    providers.add(m.provider);
  }
  const verified = mostRecent(
    llm.models,
    stt.models,
    tts.models,
    numbers,
    a2p10dlc,
    sms.providers,
  );

  return (
    <>
      <JsonLd
        data={collectionSchema({
          title: COSTS_HERO_COPY.heading,
          description: COSTS_SITE_DESCRIPTION,
          path: "/costs",
          origin: SITE_ORIGIN,
          items: [
            { name: "Language models", path: "/costs#llm" },
            { name: "Speech recognition", path: "/costs#stt" },
            { name: "Speech synthesis", path: "/costs#tts" },
            { name: "Phone numbers", path: "/costs#telephony" },
            { name: "SMS", path: "/costs#sms" },
          ],
        })}
      />
      <div className="dispatch-tape">
        <div className="wrap row">
          <div className="left">
            <span className="dot">●</span> {COSTS_HERO_COPY.badge}
          </div>
          <div className="right">updated {today} / cc-by-4.0 / v0.2.0</div>
        </div>
      </div>

      <header className="costs-hero">
        <div className="wrap">
          <div className="hero-grid">
            <div className="costs-hero-copy">
              <h1 className="dispatch-h1">{COSTS_HERO_COPY.heading}</h1>
              <p>
                Find the prices you need to estimate an AI agent’s running cost.
                Compare language and speech models, then check SMS and phone
                number rates.
              </p>
              <div className="costs-hero-actions">
                <a href="/costs/compare">Compare models</a>
                <a href="/costs/costs.md">Fetch markdown</a>
              </div>
            </div>
            <aside className="filed-panel">
              <b>Filed {today}</b>
              <span>Schema-validated public dataset.</span>
              <dl>
                <dt>Version</dt>
                <dd>2</dd>
                <dt>Verified</dt>
                <dd>{verified}</dd>
                <dt>License</dt>
                <dd>CC-BY-4.0</dd>
                <dt>Source</dt>
                <dd>
                  <a href="https://github.com/hail-hq/hail/tree/main/costs">
                    github / hail-hq
                  </a>
                </dd>
              </dl>
            </aside>
          </div>
        </div>
      </header>

      <section className="summary-strip">
        <div className="wrap">
          <div className="grid">
            <div className="stat">
              <div className="k">Models</div>
              <div className="v">{totalModels}</div>
              <div className="n">across {providers.size} providers</div>
            </div>
            <div className="stat">
              <div className="k">LLMs</div>
              <div className="v">{llm.models.length}</div>
              <div className="n">
                {rowRange(
                  llm.models,
                  "output_per_mtok_usd",
                  2,
                  0,
                  "Mtok output",
                )}
              </div>
            </div>
            <div className="stat">
              <div className="k">STT</div>
              <div className="v">{stt.models.length}</div>
              <div className="n">
                {rowRange(stt.models, "price_per_minute_usd", 6, 4, "min")}
              </div>
            </div>
            <div className="stat">
              <div className="k">TTS</div>
              <div className="v">{tts.models.length}</div>
              <div className="n">
                {rowRange(
                  tts.models,
                  "price_per_1m_chars_usd",
                  0,
                  0,
                  "1M chars",
                )}
              </div>
            </div>
            <div className="stat">
              <div className="k">SMS</div>
              <div className="v">{sms.providers.length}</div>
              <div className="n">
                {rowRange(sms.providers, "usd_per_segment", 5, 5, "segment")} ·
                base
              </div>
            </div>
            <div className="stat">
              <div className="k">Numbers</div>
              <div className="v">{numbers.length}</div>
              <div className="n">
                {priceRange(
                  numbers.filter(onSale).map((r) => r.total_usd_per_month),
                  2,
                  2,
                  "mo",
                )}{" "}
                · at cost
              </div>
            </div>
          </div>
        </div>
      </section>

      <Toolbar
        categories={[
          { id: "llm", label: "LLM" },
          { id: "stt", label: "STT" },
          { id: "tts", label: "TTS" },
          { id: "telephony", label: "Numbers" },
          { id: "sms", label: "SMS" },
        ]}
      />

      <LLMSection data={llm.models} />
      <STTSection data={stt.models} />
      <TTSSection data={tts.models} />
      <TelephonySection data={numbers} />
      <SmsSection data={sms.providers} />

      <section className="costs-data">
        <div className="wrap">
          <h2>Programmatic access</h2>
          <p>
            The full dataset is published as JSON and Markdown. Agents and
            scripts should fetch directly:
          </p>
          <ul>
            <li>
              <a href="/costs/costs.md">/costs.md</a> · markdown view (this
              page)
            </li>
            <li>
              <a href="https://raw.githubusercontent.com/hail-hq/hail/main/costs/llm.json">
                raw / costs/llm.json
              </a>
            </li>
            <li>
              <a href="https://raw.githubusercontent.com/hail-hq/hail/main/costs/stt.json">
                raw / costs/stt.json
              </a>
            </li>
            <li>
              <a href="https://raw.githubusercontent.com/hail-hq/hail/main/costs/tts.json">
                raw / costs/tts.json
              </a>
            </li>
            <li>
              Phone numbers, one file per carrier (Twilio&apos;s also holds the
              10DLC fees):{" "}
              <code>
                https://raw.githubusercontent.com/hail-hq/hail/main/costs/twilio.json
              </code>
              , <code>telnyx.json</code>, <code>didww.json</code>
            </li>
            <li>
              SMS provider base rates:{" "}
              <code>
                https://raw.githubusercontent.com/hail-hq/hail/main/costs/sms.json
              </code>
            </li>
          </ul>
          <p>
            See this data applied:{" "}
            <a href="https://hail.so/tools/voice-agent-cost-calculator">
              AI voice agent cost calculator
            </a>{" "}
            ·{" "}
            <a href="https://hail.so/tools/sms-length-calculator">
              SMS length &amp; segment calculator
            </a>
            {" — free tools built on these numbers."}
          </p>
        </div>
      </section>
    </>
  );
}
