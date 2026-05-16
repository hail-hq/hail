import { llm, stt, tts } from '@/lib/costs';
import { url } from '@/lib/url';
import { LLMSection } from '@/components/categories/llm-section';
import { STTSection } from '@/components/categories/stt-section';
import { TTSSection } from '@/components/categories/tts-section';
import { Toolbar } from '@/components/toolbar';

export const dynamic = 'force-static';

export const metadata = {
  title: 'Model costs — Hail',
  description:
    'Public, validated pricing and capability data for AI model providers — LLMs, speech-to-text, and text-to-speech. Schema-validated, CC-BY-4.0, refreshed weekly.',
  alternates: {
    types: {
      'text/markdown': '/docs/costs.md',
    },
  },
};

export default function CostsPage() {
  const today = new Date().toISOString().slice(0, 10);

  const totalModels = llm.models.length + stt.models.length + tts.models.length;
  const providers = new Set<string>();
  for (const m of [...llm.models, ...stt.models, ...tts.models]) {
    providers.add(m.provider);
  }

  return (
    <>
      <div className="dispatch-tape">
        <div className="wrap row">
          <div className="left">
            <span className="dot">●</span> HAIL.SO / DISPATCH · {today} · MODEL COSTS
          </div>
          <div className="right">
            FILE: <b>COSTS</b> · CC-BY-4.0 · v0.1.0
          </div>
        </div>
      </div>

      <header
        style={{
          padding: '40px 0 28px',
          borderBottom: '2px solid var(--color-ink)',
        }}
      >
        <div className="wrap">
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'minmax(0, 1fr) minmax(220px, 280px)',
              gap: 32,
              alignItems: 'end',
            }}
          >
            <h1 className="dispatch-h1">
              <span className="stroked">MODEL</span> COSTS
            </h1>
            <aside className="filed-panel">
              <b>FILED {today}</b>
              <span>Schema-validated public dataset.</span>
              <dl>
                <dt>VERSION</dt>
                <dd>1</dd>
                <dt>UPDATED</dt>
                <dd>{llm.updated}</dd>
                <dt>LICENSE</dt>
                <dd>CC-BY-4.0</dd>
                <dt>SOURCE</dt>
                <dd>
                  <a
                    href="https://github.com/hail-hq/hail/tree/main/costs"
                    style={{ textDecoration: 'underline' }}
                  >
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
                $
                {Math.min(...llm.models.map((m) => m.output_per_mtok_usd)).toFixed(2)} – $
                {Math.max(...llm.models.map((m) => m.output_per_mtok_usd)).toFixed(0)} / Mtok output
              </div>
            </div>
            <div className="stat">
              <div className="k">STT</div>
              <div className="v">{stt.models.length}</div>
              <div className="n">
                $
                {Math.min(...stt.models.map((m) => m.price_per_minute_usd)).toFixed(6)} – $
                {Math.max(...stt.models.map((m) => m.price_per_minute_usd)).toFixed(4)} / min
              </div>
            </div>
            <div className="stat">
              <div className="k">TTS</div>
              <div className="v">{tts.models.length}</div>
              <div className="n">
                $
                {Math.min(...tts.models.map((m) => m.price_per_1m_chars_usd)).toFixed(0)} – $
                {Math.max(...tts.models.map((m) => m.price_per_1m_chars_usd)).toFixed(0)} / 1M chars
              </div>
            </div>
          </div>
        </div>
      </section>

      <Toolbar
        categories={[
          { id: 'llm', label: 'LLM' },
          { id: 'stt', label: 'STT' },
          { id: 'tts', label: 'TTS' },
        ]}
      />

      <LLMSection data={llm.models} />
      <STTSection data={stt.models} />
      <TTSSection data={tts.models} />

      <section
        style={{
          padding: '36px 0',
          borderBottom: '2px solid var(--color-ink)',
          background: 'var(--color-paper)',
        }}
      >
        <div className="wrap">
          <h3
            style={{
              fontFamily: 'var(--font-mono)',
              fontSize: 11,
              fontWeight: 700,
              letterSpacing: '0.14em',
              textTransform: 'uppercase',
              color: 'var(--color-mute)',
              margin: '0 0 14px',
            }}
          >
            Programmatic access
          </h3>
          <p style={{ maxWidth: '60ch', margin: '0 0 18px', fontSize: 15 }}>
            The full dataset is published as JSON and Markdown. Agents and scripts should fetch
            directly:
          </p>
          <ul
            style={{
              listStyle: 'none',
              padding: 0,
              margin: 0,
              fontFamily: 'var(--font-mono)',
              fontSize: 13,
              lineHeight: 1.9,
            }}
          >
            <li>
              <a href={url('/costs.md')} style={{ textDecoration: 'underline' }}>
                /docs/costs.md
              </a>{' '}
              · markdown view (this page)
            </li>
            <li>
              <a href={url('/llms.txt')} style={{ textDecoration: 'underline' }}>
                /docs/llms.txt
              </a>{' '}
              · llmstxt.org manifest
            </li>
            <li>
              <a
                href="https://raw.githubusercontent.com/hail-hq/hail/main/costs/llm.json"
                style={{ textDecoration: 'underline' }}
              >
                raw / costs/llm.json
              </a>
            </li>
            <li>
              <a
                href="https://raw.githubusercontent.com/hail-hq/hail/main/costs/stt.json"
                style={{ textDecoration: 'underline' }}
              >
                raw / costs/stt.json
              </a>
            </li>
            <li>
              <a
                href="https://raw.githubusercontent.com/hail-hq/hail/main/costs/tts.json"
                style={{ textDecoration: 'underline' }}
              >
                raw / costs/tts.json
              </a>
            </li>
          </ul>
        </div>
      </section>
    </>
  );
}
