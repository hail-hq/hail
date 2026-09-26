import type { ReactNode } from "react";
import "./footer.css";

export type FooterGroup = {
  title: string;
  links: { label: string; href: string }[];
};

export type FooterBadge = {
  id: string;
  href: string;
  alt: string;
  /** Rendered size in px. Both are set so the row does not shift on load. */
  width: number;
  height: number;
  /** One image per footer theme. */
  src: { light: string; dark: string };
};

/** Framework-independent, server-rendered footer. Native anchors cross Next zones safely. */
export function Footer({
  groups,
  themeControl,
  badges = [],
  homeHref = "/",
}: {
  groups: FooterGroup[];
  themeControl: ReactNode;
  badges?: FooterBadge[];
  homeHref?: string;
}) {
  return (
    <footer className="hail-footer" aria-label="Hail">
      <div className="hail-footer-inner">
        <div className="hail-footer-grid">
          <div className="hail-footer-brand">
            <a className="hail-footer-mark" href={homeHref}>
              hail.so
            </a>
            <p>
              Email, SMS, and agentic phone calls for AI agents and backend
              applications. Open source or managed cloud.
            </p>
            <div className="hail-footer-social">
              <a href="https://github.com/hail-hq/hail">GitHub ↗</a>
              <a href="https://x.com/hail_hq">X ↗</a>
            </div>
          </div>
          {groups.map((group) => (
            <nav
              key={group.title}
              aria-label={group.title}
              className="hail-footer-column"
            >
              <h2>{group.title}</h2>
              {group.links.map((link) => (
                <a key={link.href} href={link.href}>
                  {link.label}
                </a>
              ))}
            </nav>
          ))}
        </div>
        {badges.length > 0 && (
          <ul className="hail-footer-badges" aria-label="Featured on">
            {badges.map((badge) => (
              <li key={badge.id}>
                <a href={badge.href} target="_blank" rel="noopener">
                  {(["light", "dark"] as const).map((theme) => (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img
                      key={theme}
                      className={`hail-footer-badge-${theme}`}
                      src={badge.src[theme]}
                      alt={badge.alt}
                      width={badge.width}
                      height={badge.height}
                      loading="lazy"
                    />
                  ))}
                </a>
              </li>
            ))}
          </ul>
        )}
        <div className="hail-footer-end">
          <span>© {new Date().getFullYear()} hail.so</span>
          <span>
            give your ai agent a voice, a real phone number, and an inbox
          </span>
          <span>MCP · CLI · REST</span>
          {themeControl}
        </div>
      </div>
    </footer>
  );
}
