import { siteHref } from "@/lib/url";

/**
 * Mirrors hail.so's SiteHeader so /costs carries the same chrome as every
 * other page behind the rewrite. The "AI Model Pricing Database" chip is this
 * page, so it renders active rather than as a link.
 */
export function Header() {
  return (
    <header className="site-header">
      <div className="row">
        <a href={siteHref("/")} className="site-mark" aria-label="Hail home">
          hail.so
        </a>
        <nav className="site-nav">
          <a href={siteHref("/compare")} className="nav-link">
            compare
          </a>
          <a href={siteHref("/pricing")} className="nav-link">
            pricing
          </a>
          <a href={siteHref("/tools")} className="nav-link">
            tools
          </a>
          <a href={siteHref("/mcp")} className="nav-link">
            mcp
          </a>
          <span className="nav-chip is-active nav-hide-lg" aria-current="page">
            database
          </span>
          <a href={siteHref("/integrations")} className="nav-link nav-hide">
            resources
          </a>
          <a href={siteHref("/docs")} className="nav-link nav-hide">
            docs
          </a>
          <a
            href="https://github.com/hail-hq/hail"
            target="_blank"
            rel="noopener"
            aria-label="github"
            className="nav-gh nav-hide"
          >
            [ github ]
          </a>
          <a href={siteHref("/signup")} className="nav-cta">
            get started
          </a>
        </nav>
      </div>
    </header>
  );
}
