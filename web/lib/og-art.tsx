const INK = '#0d0d0d';
const BG = '#e9e7e3';
const ACCENT = '#c4362c';
const MUTE = '#6e6b66';

export const OG_SIZE = { width: 1200, height: 630 };

/** Mirrors hail.so's own opengraph-image.tsx layout (accent square + wordmark,
 * headline, tagline/domain footer) and palette, so /costs OG cards read as
 * the same site rather than a neighbouring property. The default Satori
 * sans is acceptable; do not fetch remote fonts here. */
export function OgArt() {
  return (
    <div
      style={{
        width: '100%',
        height: '100%',
        display: 'flex',
        flexDirection: 'column',
        justifyContent: 'space-between',
        background: BG,
        color: INK,
        padding: 72,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
        <div style={{ width: 22, height: 22, background: ACCENT }} />
        <div style={{ fontSize: 26, letterSpacing: 6, textTransform: 'uppercase' }}>Hail</div>
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
        Every AI model&#39;s price, verified weekly.
      </div>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          fontSize: 26,
        }}
      >
        <div style={{ color: ACCENT }}>Schema-validated · CC-BY-4.0</div>
        <div style={{ color: MUTE }}>hail.so/costs</div>
      </div>
    </div>
  );
}
