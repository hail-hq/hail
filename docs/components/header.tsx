import Link from 'next/link';

export function Header() {
  return (
    <header className="site-header">
      <div className="wrap row">
        <div className="left">
          <Link href="/" className="brand-mark">
            hail<i>.so</i>
          </Link>
          <span className="doc-tag">DOCS</span>
        </div>
        <a href="https://hail.so" className="back">
          back to hail.so
        </a>
      </div>
    </header>
  );
}
