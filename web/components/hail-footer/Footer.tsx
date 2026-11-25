import type { ReactNode } from "react";
import { Mascot } from "./Mascot";
import { PRODUCT_COPY } from "./product-copy";
import { SocialLinks } from "./SocialLinks";
import { FooterScene } from "./FooterScene";
import { FooterAccordion } from "./FooterAccordion";
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
  icon?: string;
  /** The directory asks for a website link in exchange for listing Hail. */
  reciprocal?: boolean;
};

function FooterLinks({ group }: { group: FooterGroup }) {
  return (
    <nav aria-label={group.title}>
      {group.links.map((link) => (
        <a key={link.href} href={link.href}>{link.label}</a>
      ))}
    </nav>
  );
}

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
              <Mascot className="hail-footer-mascot" size={36} />
              <span>hail</span>
            </a>
            <p>
              {PRODUCT_COPY.tagline} {PRODUCT_COPY.availability}
            </p>
            <SocialLinks />
          </div>
          {groups.map((group) => (
            <details key={group.title} className="hail-footer-column">
              <summary><h2>{group.title}</h2><span className="hail-footer-plus" aria-hidden="true" /></summary>
              <FooterLinks group={group} />
            </details>
          ))}
          <FooterAccordion />
        </div>
        {badges.length > 0 && (
          <ul id="footer-badges" className="hail-footer-badges" aria-label="Directory and platform links">
            {badges.map((badge) => (
              <li key={badge.id}>
                <a
                  href={badge.href}
                  target="_blank"
                  rel={badge.reciprocal ? "noopener sponsored" : "noopener"}
                  aria-label={badge.alt}
                >
                  {badge.icon && (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img src={badge.icon} alt="" width={36} height={36} loading="lazy" />
                  )}
                  <span className="hail-footer-badge-copy">
                    <span className="hail-footer-badge-label">{badge.reciprocal ? "Directory" : "Find Hail on"}</span>
                    <span className="hail-footer-badge-name">{badge.name}</span>
                  </span>
                </a>
              </li>
            ))}
          </ul>
        )}
        <div className="hail-footer-end">
          <span>© {new Date().getFullYear()} hail</span>
          <div className="hail-footer-theme">{themeControl}</div>
        </div>
      </div>
      <FooterScene />
    </footer>
  );
}
