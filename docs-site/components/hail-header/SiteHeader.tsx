"use client";

import { useId, useRef, type ReactNode } from "react";
import { useDropdown } from "./useDropdown";
import { Mascot } from "./Mascot";
import styles from "./SiteHeader.module.css";

export type SiteHeaderActive = "docs" | null;

const RESOURCES = [
  {
    label: "Product",
    items: [
      { label: "Voice API", href: "/voice" },
      { label: "SMS API", href: "/sms" },
      { label: "Email API", href: "/email" },
      { label: "Integrations", href: "/integrations" },
      { label: "MCP connections", href: "/mcp" },
    ],
  },
  {
    label: "Guides",
    items: [
      { label: "Engineering blog", href: "/blog" },
      { label: "10DLC timeline guide", href: "/sms#10dlc-registration" },
      { label: "Domain warmup guide", href: "/email#domain-warmup" },
      { label: "Compare platforms", href: "/compare" },
      { label: "Free tools", href: "/tools" },
      { label: "AI model pricing", href: "/costs" },
    ],
  },
];

function navLinkClass(isActive: boolean, extra?: string) {
  const classes = [styles.navLink];
  if (isActive) classes.push(styles.active);
  if (extra) classes.push(extra);
  return classes.join(" ");
}

/** Shared marketing navigation with a collapsible mobile menu. */
export function SiteHeader({
  active = null,
  themeControl,
  origin = "https://hail.so",
}: {
  active?: SiteHeaderActive;
  themeControl: ReactNode;
  origin?: string;
}) {
  const siteHref = (path: string) => new URL(path, origin).href;
  const { open, setOpen, ref } = useDropdown();
  const {
    open: mobileOpen,
    setOpen: setMobileOpen,
    ref: mobileRef,
  } = useDropdown();
  const menuButton = useRef<HTMLButtonElement>(null);
  const navId = useId();

  return (
    <header className={styles.header}>
      <div
        className={styles.shell}
        ref={mobileRef}
        onKeyDown={(event) => {
          if (event.key === "Escape" && mobileOpen) {
            setMobileOpen(false);
            setOpen(false);
            menuButton.current?.focus();
          }
          // The open phone menu covers the page: Tab cycles between the toggle and the menu links.
          if (event.key === "Tab" && mobileOpen && menuButton.current) {
            const nav = document.getElementById(navId);
            const items = [
              menuButton.current,
              ...(nav
                ? Array.from(nav.querySelectorAll<HTMLElement>("a, button"))
                : []),
            ];
            // The toggle is last in the DOM, so step through this list instead of letting Tab leave it.
            const at = items.indexOf(document.activeElement as HTMLElement);
            if (at >= 0) {
              event.preventDefault();
              items[
                (at + (event.shiftKey ? items.length - 1 : 1)) % items.length
              ].focus();
            }
          }
        }}
      >
        <a className={styles.logo} href={siteHref("/")} aria-label="hail home">
          <Mascot size={26} />
          hail.so
        </a>
        <nav
          id={navId}
          className={`${styles.nav} ${mobileOpen ? styles.navOpen : ""}`}
          aria-label="main navigation"
          onClick={(event) => {
            if ((event.target as HTMLElement).closest("a")) {
              setMobileOpen(false);
              setOpen(false);
            }
          }}
        >
          <div className={styles.dropdown} ref={ref}>
            <button
              type="button"
              className={navLinkClass(false)}
              aria-haspopup="true"
              aria-expanded={open}
              onClick={() => setOpen(!open)}
            >
              Product <span aria-hidden="true">{open ? "▴" : "▾"}</span>
            </button>
            {open && (
              <div className={styles.panel}>
                {RESOURCES.map((group) => (
                  <div key={group.label}>
                    <div className={styles.panelGroup}>{group.label}</div>
                    {group.items.map((item) => (
                      <a
                        key={item.href}
                        href={siteHref(item.href)}
                        onClick={() => setOpen(false)}
                      >
                        {item.label}
                      </a>
                    ))}
                  </div>
                ))}
              </div>
            )}
          </div>
          <a href={siteHref("/pricing")} className={navLinkClass(false)}>
            Pricing
          </a>
          <a
            href={siteHref("/docs")}
            aria-current={active === "docs" ? "page" : undefined}
            className={navLinkClass(active === "docs")}
          >
            Docs
          </a>
          <a href={siteHref("/blog")} className={navLinkClass(false)}>
            Resources
          </a>
          <a
            className={`${styles.primary} ${styles.menuCta}`}
            href={siteHref("/signup")}
          >
            Get started
          </a>
        </nav>
        <div className={styles.navActions}>
          <div className={styles.themeButton}>{themeControl}</div>
          <a className={styles.account} href={siteHref("/signin")}>
            Sign in
          </a>
          <a className={styles.primary} href={siteHref("/signup")}>
            Get started
          </a>
          <button
            ref={menuButton}
            type="button"
            className={`${styles.menuToggle} ${mobileOpen ? styles.menuOpen : ""}`}
            aria-label={mobileOpen ? "Close navigation" : "Open navigation"}
            aria-expanded={mobileOpen}
            aria-controls={navId}
            onClick={() => {
              setMobileOpen(!mobileOpen);
              setOpen(false);
            }}
          >
            <span aria-hidden="true" />
            <span aria-hidden="true" />
            <span aria-hidden="true" />
          </button>
        </div>
      </div>
    </header>
  );
}
