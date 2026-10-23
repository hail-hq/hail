import type { ReactNode } from "react";
import { Mascot } from "./Mascot";
import { PRODUCT_COPY, PRODUCT_SHORT_DESCRIPTION } from "./product-copy";
import "./footer.css";

export type FooterGroup = {
  title: string;
  links: { label: string; href: string }[];
};

export type FooterBadge = {
  id: string;
  href: string;
  alt: string;
  name: string;
  /** Square provider logo. Shared CSS owns all card and logo dimensions. */
  icon: string;
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
            {/* The same Mascot as the header: it blinks, and nods when this link is hovered. */}
            <a className="hail-footer-mark" href={homeHref}>
              <Mascot className="hail-footer-mascot" size={64} />
              <span>hail.so</span>
            </a>
            <p>
              {PRODUCT_SHORT_DESCRIPTION} {PRODUCT_COPY.availability}
            </p>
            <div className="hail-footer-social">
              <a href="https://github.com/hail-hq/hail">GitHub ↗</a>
              <a href="https://x.com/redouaneoachour">X ↗</a>
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
          <ul id="footer-badges" className="hail-footer-badges" aria-label="Featured on">
            {badges.map((badge) => (
              <li key={badge.id}>
                <a href={badge.href} target="_blank" rel="noopener" aria-label={badge.alt}>
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img src={badge.icon} alt="" width={36} height={36} loading="lazy" />
                  <span className="hail-footer-badge-copy">
                    <span className="hail-footer-badge-label">FEATURED ON</span>
                    <span className="hail-footer-badge-name">{badge.name}</span>
                  </span>
                </a>
              </li>
            ))}
          </ul>
        )}
        <div className="hail-footer-end">
          <span>© {new Date().getFullYear()} hail.so</span>
          {themeControl}
        </div>
      </div>
    </footer>
  );
}
