import { siteHref } from "@/lib/url";
import { GitHubIcon, XIcon } from "./icons";

/** Mirrors hail.so's slim SiteFooter; the end bar carries the dataset's own
 *  filing line (version, licence, source) in place of the site's. */
export function Footer() {
  return (
    <footer className="site-foot">
      <div className="wrap">
        <div className="foot-top">
          <a
            href={siteHref("/")}
            className="site-mark"
            style={{ fontSize: 20 }}
          >
            hail.so
          </a>
          <nav className="foot-nav">
            <a href={siteHref("/#channels")}>Channels</a>
            <a href={siteHref("/integrations")}>Integrations</a>
            <a href={siteHref("/compare")}>Compare</a>
            <a href={siteHref("/pricing")}>Pricing</a>
            <a href={siteHref("/tools")}>Free tools</a>
            <a href={siteHref("/docs")}>Docs</a>
            <a href={siteHref("/mcp")}>MCP</a>
            <a href={siteHref("/skill.md")}>For agents</a>
          </nav>
          <div style={{ display: "flex", gap: 8 }}>
            <a
              className="foot-soc"
              href="https://github.com/hail-hq/hail"
              target="_blank"
              rel="noopener"
              aria-label="GitHub"
            >
              <GitHubIcon />
            </a>
            <a
              className="foot-soc"
              href="https://x.com/hail_hq"
              target="_blank"
              rel="noopener"
              aria-label="Hail on X"
            >
              <XIcon />
            </a>
          </div>
        </div>
        <div className="foot-end">
          <span>
            <span className="dot">●</span> END DISPATCH · HAIL.SO / COSTS ·
            v0.2.0 · CC-BY-4.0 ·{" "}
            <a href="https://github.com/hail-hq/hail/tree/main/costs">source</a>
          </span>
          <span className="tagline">
            phone + sms + email for ai agents.
          </span>
          <span>MCP · CLI · REST</span>
        </div>
      </div>
    </footer>
  );
}
