import { url } from '@/lib/url';

export function Toolbar({ categories }: { categories: { id: string; label: string }[] }) {
  return (
    <div className="toolbar">
      <div className="wrap row">
        <div className="anchors">
          {categories.map((c) => (
            <a key={c.id} href={`#${c.id}`}>
              {c.label}
            </a>
          ))}
        </div>
        <div style={{ display: 'flex', gap: 8, marginLeft: 'auto' }}>
          <a className="btn btn-outline" href={url('/costs/compare')}>
            Compare
          </a>
          <a className="btn btn-outline" href={url('/llms.txt')}>
            /llms.txt
          </a>
          <a className="btn btn-filled" href={url('/costs.md')}>
            ↓ .md
          </a>
        </div>
      </div>
    </div>
  );
}
