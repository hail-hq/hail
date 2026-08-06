import { siteHref } from "@/lib/url";
import { GitHubIcon } from "./icons";

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
          HAIL<span>.SO</span>
        </a>
        <nav className="site-nav">
          <a href={siteHref("/compare")} className="nav-link">
            Compare
          </a>
          <a href={siteHref("/pricing")} className="nav-link">
            Pricing
          </a>
          <a href={siteHref("/tools")} className="nav-link">
            Tools
          </a>
          <a href={siteHref("/mcp")} className="nav-link">
            MCP
          </a>
          <a href={siteHref("/docs")} className="nav-link nav-hide">
            Docs
          </a>
          <span className="nav-chip is-active nav-hide-lg" aria-current="page">
            AI Model Pricing Database
          </span>
          <a
            href="https://github.com/hail-hq/hail"
            target="_blank"
            rel="noopener"
            aria-label="GitHub"
            className="nav-gh nav-hide"
          >
            <GitHubIcon />
          </a>
          <a href={siteHref("/signup")} className="nav-cta">
            Get started →
          </a>
        </nav>
      </div>
    </header>
  );
}
