"use client";

import { useId, useRef, type ReactNode } from "react";
import { useDropdown } from "./useDropdown";
import styles from "./SiteHeader.module.css";

export type SiteHeaderActive = "costs" | "docs" | null;

const RESOURCES = [
  {
    label: "product",
    items: [
      { label: "Voice API", href: "/voice" },
      { label: "SMS API", href: "/sms" },
      { label: "Email API", href: "/email" },
      { label: "integrations", href: "/integrations" },
      { label: "MCP connections", href: "/mcp" },
    ],
  },
  {
    label: "guides",
    items: [
      { label: "engineering blog", href: "/blog" },
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
        }}
      >
        <a className={styles.logo} href={siteHref("/")} aria-label="hail home">
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
        </nav>
        <div className={styles.navActions}>
          <div className={styles.themeButton}>{themeControl}</div>
          <a className={styles.primary} href={siteHref("/signup")}>
            Create account
          </a>
          <button
            ref={menuButton}
            type="button"
            className={styles.menuToggle}
            aria-label={mobileOpen ? "Close navigation" : "Open navigation"}
            aria-expanded={mobileOpen}
            aria-controls={navId}
            onClick={() => {
              setMobileOpen(!mobileOpen);
              setOpen(false);
            }}
          >
            <svg
              width="20"
              height="20"
              viewBox="0 0 20 20"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              aria-hidden="true"
            >
              {mobileOpen ? (
                <path d="m5 5 10 10M15 5 5 15" />
              ) : (
                <path d="M3 5h14M3 10h14M3 15h14" />
              )}
            </svg>
          </button>
        </div>
      </div>
    </header>
  );
}
