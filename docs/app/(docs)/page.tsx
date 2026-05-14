import Link from 'next/link';
import { url } from '@/lib/url';

export const metadata = {
  title: 'Hail Docs',
  description:
    'Documentation and pricing data for Hail — the universal communication platform for AI agents.',
};

export default function HomePage() {
  const today = new Date().toISOString().slice(0, 10);
  return (
    <>
      <div className="dispatch-tape">
        <div className="wrap row">
          <div className="left">
            <span className="dot">●</span> HAIL.SO / DOCS · {today}
          </div>
          <div className="right">
            FILE: <b>INDEX</b> · v0.1.0
          </div>
        </div>
      </div>

      <header
        style={{
          padding: '64px 0 56px',
          borderBottom: '2px solid var(--color-ink)',
        }}
      >
        <div className="wrap">
          <h1 className="dispatch-h1" style={{ maxWidth: '14ch' }}>
            Hail <em className="it">docs.</em>
          </h1>
          <p
            style={{
              marginTop: 28,
              fontSize: 21,
              lineHeight: 1.4,
              maxWidth: '60ch',
            }}
          >
            Documentation and pricing data for Hail — the universal communication platform for AI
            agents. Outbound calls, SMS, and email with one CLI and one MCP endpoint.
          </p>
        </div>
      </header>

      <section
        style={{
          padding: '48px 0',
          borderBottom: '2px solid var(--color-ink)',
          background: 'var(--color-paper)',
        }}
      >
        <div className="wrap">
          <h2
            style={{
              fontFamily: 'var(--font-mono)',
              fontSize: 11,
              fontWeight: 700,
              letterSpacing: '0.14em',
              textTransform: 'uppercase',
              color: 'var(--color-mute)',
              margin: '0 0 24px',
            }}
          >
            Available
          </h2>
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))',
              gap: 0,
              border: '2px solid var(--color-ink)',
              maxWidth: 960,
            }}
          >
            <Link
              href="/costs"
              style={{
                display: 'block',
                padding: '24px 28px',
                background: 'var(--color-bg)',
                borderRight: '2px solid var(--color-ink)',
              }}
            >
              <div
                style={{
                  fontFamily: 'var(--font-mono)',
                  fontSize: 11,
                  fontWeight: 700,
                  letterSpacing: '0.14em',
                  textTransform: 'uppercase',
                  color: 'var(--color-mute)',
                }}
              >
                01 / Dispatch
              </div>
              <div
                style={{
                  fontSize: 32,
                  fontWeight: 800,
                  letterSpacing: '-0.03em',
                  lineHeight: 1,
                  marginTop: 8,
                  textTransform: 'uppercase',
                }}
              >
                Model <em className="it">costs.</em> →
              </div>
              <p style={{ marginTop: 12, fontSize: 14, color: 'var(--color-mute)' }}>
                Public pricing data across LLM, STT, and TTS providers. Schema-validated,
                refreshed weekly, CC-BY-4.0.
              </p>
            </Link>
            {/* /mcp lives in the landing zone, cross-zone <a> avoids wasted Next.js prefetch */}
            <a
              href="/mcp"
              style={{
                display: 'block',
                padding: '24px 28px',
                background: 'var(--color-paper)',
              }}
            >
              <div
                style={{
                  fontFamily: 'var(--font-mono)',
                  fontSize: 11,
                  fontWeight: 700,
                  letterSpacing: '0.14em',
                  textTransform: 'uppercase',
                  color: 'var(--color-mute)',
                }}
              >
                02 / Connect
              </div>
              <div
                style={{
                  fontSize: 32,
                  fontWeight: 800,
                  letterSpacing: '-0.03em',
                  lineHeight: 1,
                  marginTop: 8,
                  textTransform: 'uppercase',
                }}
              >
                MCP <em className="it">clients.</em> →
              </div>
              <p style={{ marginTop: 12, fontSize: 14, color: 'var(--color-mute)' }}>
                Drop the Hail MCP server into Claude, ChatGPT, Cursor, Gemini, and 4 more —
                one-click setup snippets.
              </p>
            </a>
          </div>
        </div>
      </section>

      <section style={{ padding: '48px 0', borderBottom: '2px solid var(--color-ink)' }}>
        <div className="wrap">
          <h2
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
            For agents
          </h2>
          <p style={{ maxWidth: '60ch', margin: 0, fontSize: 15 }}>
            Skip the prose. The dataset is also published in machine-readable formats:
          </p>
          <ul
            style={{
              listStyle: 'none',
              padding: 0,
              marginTop: 18,
              fontFamily: 'var(--font-mono)',
              fontSize: 13,
              lineHeight: 1.9,
            }}
          >
            <li>
              <a href={url('/llms.txt')} style={{ textDecoration: 'underline' }}>
                /docs/llms.txt
              </a>{' '}
              · llmstxt.org manifest
            </li>
            <li>
              <a href={url('/costs.md')} style={{ textDecoration: 'underline' }}>
                /docs/costs.md
              </a>{' '}
              · costs as markdown
            </li>
            <li>
              <a
                href="https://github.com/hail-hq/hail/tree/main/costs"
                style={{ textDecoration: 'underline' }}
              >
                costs/*.json
              </a>{' '}
              · raw schema-validated JSON
            </li>
          </ul>
        </div>
      </section>

      <section style={{ padding: '36px 0' }}>
        <div className="wrap">
          <h2
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
            Reference
          </h2>
          <ul
            style={{
              listStyle: 'none',
              padding: 0,
              margin: 0,
              fontSize: 15,
              lineHeight: 2,
            }}
          >
            <li>
              · <Link href="/architecture" style={{ textDecoration: 'underline' }}>Architecture overview</Link>
            </li>
            <li>
              · <Link href="/contributing" style={{ textDecoration: 'underline' }}>Contributing guide</Link>
            </li>
            <li style={{ color: 'var(--color-mute)' }}>
              · Setup guides, ops runbooks, OpenAPI reference — <em className="it">coming soon</em>
            </li>
          </ul>
        </div>
      </section>
    </>
  );
}
