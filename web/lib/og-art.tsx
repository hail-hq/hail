const INK = '#0d0d0d';
const BG = '#e9e7e3';
const PAPER = '#f2f0ec';
const MUTE = '#6e6b66';
const ACCENT = '#c4362c';

export const OG_SIZE = { width: 1200, height: 630 };

export function OgArt() {
  return (
    <div
      style={{
        width: '100%',
        height: '100%',
        display: 'flex',
        flexDirection: 'column',
        background: BG,
        color: INK,
        fontFamily: 'sans-serif',
      }}
    >
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          padding: '28px 56px',
          fontSize: 20,
          letterSpacing: 2,
          textTransform: 'uppercase',
          fontWeight: 700,
          borderBottom: `3px solid ${INK}`,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center' }}>
          <span style={{ color: ACCENT, marginRight: 14 }}>●</span>
          HAIL.SO / DISPATCH
        </div>
        <div style={{ color: MUTE }}>CC-BY-4.0 · WEEKLY</div>
      </div>

      <div
        style={{
          display: 'flex',
          flexDirection: 'column',
          justifyContent: 'center',
          flex: 1,
          padding: '0 56px',
        }}
      >
        <div
          style={{
            fontSize: 128,
            fontWeight: 800,
            letterSpacing: -4,
            lineHeight: 0.92,
            textTransform: 'uppercase',
          }}
        >
          Model Costs
        </div>
        <div
          style={{
            display: 'flex',
            marginTop: 28,
            fontSize: 26,
            color: MUTE,
          }}
        >
          Public, validated AI pricing — LLM · STT · TTS · SMS · Telephony
        </div>
      </div>

      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          padding: '26px 56px',
          background: PAPER,
          borderTop: `2px solid ${INK}`,
          fontSize: 22,
          fontWeight: 700,
          letterSpacing: 1,
        }}
      >
        <div style={{ display: 'flex' }}>hail.so/costs</div>
        <div style={{ display: 'flex', color: ACCENT }}>SCHEMA-VALIDATED</div>
      </div>
    </div>
  );
}
