import Link from 'next/link';
import { llm, stt, tts } from '@/lib/costs';
import {
  LLMCompareTable,
  STTCompareTable,
  TTSCompareTable,
} from '@/components/compare-table';

export const metadata = {
  title: 'Compare model costs — Hail',
  description: 'Compare AI model providers side-by-side. Schema-validated, refreshed weekly.',
};

const MAX_COMPARE = 6;

interface ComparePageProps {
  searchParams: Promise<{ m?: string }>;
}

function buildAddUrl(currentIds: string[], idToAdd: string): string {
  if (currentIds.includes(idToAdd) || currentIds.length >= MAX_COMPARE) {
    return `/costs/compare?m=${currentIds.join(',')}`;
  }
  const next = [...currentIds, idToAdd];
  return `/costs/compare?m=${next.join(',')}`;
}

export default async function ComparePage({ searchParams }: ComparePageProps) {
  const today = new Date().toISOString().slice(0, 10);
  const params = await searchParams;
  const requestedIds = (params.m ?? '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);

  const selectedLLM = requestedIds
    .map((id) => llm.models.find((m) => m.model_id === id))
    .filter((m): m is (typeof llm.models)[number] => Boolean(m));
  const selectedSTT = requestedIds
    .map((id) => stt.models.find((m) => m.model_id === id))
    .filter((m): m is (typeof stt.models)[number] => Boolean(m));
  const selectedTTS = requestedIds
    .map((id) => tts.models.find((m) => m.model_id === id))
    .filter((m): m is (typeof tts.models)[number] => Boolean(m));

  const totalSelected = selectedLLM.length + selectedSTT.length + selectedTTS.length;
  const currentIds = [...selectedLLM, ...selectedSTT, ...selectedTTS].map((m) => m.model_id);

  const unselectedLLM = llm.models.filter((m) => !currentIds.includes(m.model_id));
  const unselectedSTT = stt.models.filter((m) => !currentIds.includes(m.model_id));
  const unselectedTTS = tts.models.filter((m) => !currentIds.includes(m.model_id));

  return (
    <>
      <div className="dispatch-tape">
        <div className="wrap row">
          <div className="left">
            <span className="dot">●</span> HAIL.SO / DISPATCH · {today} · COMPARE
          </div>
          <div className="right">
            FILE: <b>COMPARE</b> · {totalSelected} of {MAX_COMPARE} slots
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
              <em className="it">side</em> by side.
            </h1>
            <aside className="filed-panel">
              <b>COMPARE</b>
              <span>Up to {MAX_COMPARE} models, side-by-side.</span>
              <dl>
                <dt>SELECTED</dt>
                <dd>{totalSelected}</dd>
                <dt>LLM</dt>
                <dd>{selectedLLM.length}</dd>
                <dt>STT</dt>
                <dd>{selectedSTT.length}</dd>
                <dt>TTS</dt>
                <dd>{selectedTTS.length}</dd>
              </dl>
            </aside>
          </div>
        </div>
      </header>

      <div className="toolbar">
        <div className="wrap row">
          <Link href="/costs" className="btn btn-outline">
            ← all costs
          </Link>
          {totalSelected > 0 && (
            <a href="/costs/compare" className="btn btn-outline" rel="nofollow">
              clear
            </a>
          )}
          <div style={{ marginLeft: 'auto' }} className="anchors">
            {selectedLLM.length > 0 && <a href="#cmp-llm">LLM</a>}
            {selectedSTT.length > 0 && <a href="#cmp-stt">STT</a>}
            {selectedTTS.length > 0 && <a href="#cmp-tts">TTS</a>}
          </div>
        </div>
      </div>

      {totalSelected === 0 ? (
        <EmptyState
          llm={llm.models}
          stt={stt.models}
          tts={tts.models}
          currentIds={currentIds}
        />
      ) : (
        <>
          {selectedLLM.length > 0 && (
            <CompareSection
              id="cmp-llm"
              num="01"
              title={
                <>
                  Large language <em className="it">models</em>
                </>
              }
              count={selectedLLM.length}
            >
              <LLMCompareTable models={selectedLLM} currentIds={currentIds} />
            </CompareSection>
          )}

          {selectedSTT.length > 0 && (
            <CompareSection
              id="cmp-stt"
              num="02"
              title={
                <>
                  <em className="it">Speech</em> to text
                </>
              }
              count={selectedSTT.length}
            >
              <STTCompareTable models={selectedSTT} currentIds={currentIds} />
            </CompareSection>
          )}

          {selectedTTS.length > 0 && (
            <CompareSection
              id="cmp-tts"
              num="03"
              title={
                <>
                  Text to <em className="it">speech</em>
                </>
              }
              count={selectedTTS.length}
            >
              <TTSCompareTable models={selectedTTS} currentIds={currentIds} />
            </CompareSection>
          )}

          {totalSelected < MAX_COMPARE && (
            <AddMoreSection
              unselectedLLM={unselectedLLM}
              unselectedSTT={unselectedSTT}
              unselectedTTS={unselectedTTS}
              currentIds={currentIds}
              buildAddUrl={buildAddUrl}
            />
          )}
        </>
      )}
    </>
  );
}

function CompareSection({
  id,
  num,
  title,
  count,
  children,
}: {
  id: string;
  num: string;
  title: React.ReactNode;
  count: number;
  children: React.ReactNode;
}) {
  return (
    <section className="cat" id={id}>
      <div className="wrap">
        <div className="cat-bar">
          <span className="num">{num}</span>
          <h2>{title}</h2>
          <span className="count">
            {count} {count === 1 ? 'model' : 'models'}
          </span>
        </div>
        {children}
      </div>
    </section>
  );
}

function AddMoreSection({
  unselectedLLM,
  unselectedSTT,
  unselectedTTS,
  currentIds,
  buildAddUrl,
}: {
  unselectedLLM: { provider: string; display_name: string; model_id: string }[];
  unselectedSTT: { provider: string; display_name: string; model_id: string }[];
  unselectedTTS: { provider: string; display_name: string; model_id: string }[];
  currentIds: string[];
  buildAddUrl: (ids: string[], id: string) => string;
}) {
  return (
    <section
      style={{
        padding: '36px 0',
        borderBottom: '2px solid var(--color-ink)',
        background: 'var(--color-paper)',
      }}
      id="cmp-add"
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
            margin: '0 0 16px',
          }}
        >
          Add another model
        </h3>
        <ModelGroup label="LLMs" models={unselectedLLM} currentIds={currentIds} buildAddUrl={buildAddUrl} />
        <ModelGroup label="Speech-to-Text" models={unselectedSTT} currentIds={currentIds} buildAddUrl={buildAddUrl} />
        <ModelGroup label="Text-to-Speech" models={unselectedTTS} currentIds={currentIds} buildAddUrl={buildAddUrl} />
      </div>
    </section>
  );
}

function ModelGroup({
  label,
  models,
  currentIds,
  buildAddUrl,
}: {
  label: string;
  models: { provider: string; display_name: string; model_id: string }[];
  currentIds: string[];
  buildAddUrl: (ids: string[], id: string) => string;
}) {
  if (models.length === 0) return null;
  return (
    <div style={{ marginBottom: 18 }}>
      <div
        style={{
          fontFamily: 'var(--font-mono)',
          fontSize: 10,
          fontWeight: 700,
          letterSpacing: '0.12em',
          textTransform: 'uppercase',
          color: 'var(--color-mute)',
          marginBottom: 8,
        }}
      >
        {label}
      </div>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        {models.map((m) => (
          <a
            key={m.model_id}
            className="add-pill"
            href={buildAddUrl(currentIds, m.model_id)}
            rel="nofollow"
          >
            <span className="add-pill-plus">+</span>
            <span>
              <span className="add-pill-prov">{m.provider}</span> {m.display_name}
            </span>
          </a>
        ))}
      </div>
    </div>
  );
}

function EmptyState({
  llm,
  stt,
  tts,
  currentIds,
}: {
  llm: { provider: string; display_name: string; model_id: string }[];
  stt: { provider: string; display_name: string; model_id: string }[];
  tts: { provider: string; display_name: string; model_id: string }[];
  currentIds: string[];
}) {
  return (
    <section style={{ padding: '40px 0', borderBottom: '2px solid var(--color-ink)' }}>
      <div className="wrap">
        <div
          style={{
            border: '2px dashed var(--color-ink)',
            padding: '32px 28px',
            background: 'var(--color-paper)',
            textAlign: 'center',
            marginBottom: 36,
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
              marginBottom: 8,
            }}
          >
            No models selected
          </div>
          <p
            style={{
              fontFamily: 'var(--font-serif)',
              fontStyle: 'italic',
              fontSize: 22,
              margin: 0,
              maxWidth: '50ch',
              marginInline: 'auto',
            }}
          >
            Pick at least two models below to see them side-by-side.
          </p>
        </div>

        <h3
          style={{
            fontFamily: 'var(--font-mono)',
            fontSize: 11,
            fontWeight: 700,
            letterSpacing: '0.14em',
            textTransform: 'uppercase',
            color: 'var(--color-mute)',
            margin: '0 0 16px',
          }}
        >
          Available models
        </h3>
        <ModelGroup label="LLMs" models={llm} currentIds={currentIds} buildAddUrl={buildAddUrl} />
        <ModelGroup label="Speech-to-Text" models={stt} currentIds={currentIds} buildAddUrl={buildAddUrl} />
        <ModelGroup label="Text-to-Speech" models={tts} currentIds={currentIds} buildAddUrl={buildAddUrl} />
      </div>
    </section>
  );
}
