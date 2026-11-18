"use client";

import { useEffect, useId, useRef, type ReactNode } from "react";
import { AnswerButton } from "./AnswerButton";
import { useDropdown } from "./useDropdown";
import { Mascot } from "./Mascot";
import styles from "./SiteHeader.module.css";

export type SiteHeaderActive =
  | "product"
  | "setup"
  | "pricing"
  | "docs"
  | "blog"
  | "contact"
  | null;

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
    label: "Compare and estimate",
    items: [
      { label: "Compare platforms", href: "/compare" },
      { label: "Free tools", href: "/tools" },
      { label: "AI model pricing", href: "/costs" },
    ],
  },
];

const SETUP_GUIDES = [
  {
    label: "Start here",
    items: [{ label: "Create an AI agent", href: "/create-ai-agent" }],
  },
  {
    label: "Agent templates",
    items: [
      { label: "Front desk", href: "/agent-templates/front-desk" },
      { label: "Appointments", href: "/agent-templates/appointments" },
      { label: "Customer support", href: "/agent-templates/customer-support" },
      { label: "Sales inquiries", href: "/agent-templates/sales-inquiries" },
      { label: "Delivery updates", href: "/agent-templates/delivery-updates" },
      { label: "After hours", href: "/agent-templates/after-hours" },
      { label: "Inbox", href: "/agent-templates/inbox" },
    ],
  },
  {
    label: "Phone and email",
    items: [
      { label: "10DLC timeline guide", href: "/sms#10dlc-registration" },
      { label: "Domain warmup guide", href: "/email#domain-warmup" },
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
  loggedIn = false,
  accountAction,
  themeControl,
  origin = "https://hail.so",
}: {
  active?: SiteHeaderActive;
  loggedIn?: boolean;
  themeControl: ReactNode;
  origin?: string;
  accountAction?: { href: string; label: string };
}) {
  const current = active;
  const siteHref = (path: string) => new URL(path, origin).href;
  const { open, setOpen, ref } = useDropdown();
  const {
    open: setupOpen,
    setOpen: setSetupOpen,
    ref: setupRef,
  } = useDropdown();
  const {
    open: mobileOpen,
    setOpen: setMobileOpen,
    ref: mobileRef,
  } = useDropdown();
  const menuButton = useRef<HTMLButtonElement>(null);
  const navId = useId();

  useEffect(() => {
    const media = window.matchMedia?.("(max-width: 900px)");
    if (!mobileOpen || !media?.matches) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const closeOnDesktop = () => {
      if (!media.matches) setMobileOpen(false);
    };
    media.addEventListener("change", closeOnDesktop);
    return () => {
      document.body.style.overflow = previous;
      media.removeEventListener("change", closeOnDesktop);
    };
  }, [mobileOpen, setMobileOpen]);

  return (
    <header className={styles.header} data-public-header>
      <div
        className={styles.shell}
        ref={mobileRef}
        onKeyDown={(event) => {
          if (event.key === "Escape" && mobileOpen) {
            setMobileOpen(false);
            setOpen(false);
            setSetupOpen(false);
            menuButton.current?.focus();
          }
          // The open phone menu covers the page: Tab cycles between the toggle and the menu links.
          if (event.key === "Tab" && mobileOpen && menuButton.current) {
            const nav = document.getElementById(navId);
            const shown = (el: HTMLElement) => {
              const style = getComputedStyle(el);
              return style.display !== "none" && style.visibility !== "hidden";
            };
            const items = [
              menuButton.current,
              ...(nav
                ? Array.from(nav.querySelectorAll<HTMLElement>("a, button"))
                : []),
            ].filter(shown);
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
              setSetupOpen(false);
            }
          }}
        >
          <div
            className={styles.dropdown}
            ref={ref}
            onKeyDown={(event) => {
              if (event.key === "Escape" && open) {
                setOpen(false);
                ref.current?.querySelector("button")?.focus();
              }
            }}
          >
            <button
              type="button"
              className={navLinkClass(current === "product")}
              aria-current={current === "product" ? "true" : undefined}
              aria-haspopup="true"
              aria-expanded={open}
              onClick={() => {
                setOpen(!open);
                setSetupOpen(false);
              }}
            >
              Product
              <svg
                className={styles.chev}
                width="10"
                height="10"
                viewBox="0 0 10 10"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
                strokeLinejoin="round"
                aria-hidden="true"
              >
                <path d="M2 3.5 5 6.5 8 3.5" />
              </svg>
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
          <div
            className={styles.dropdown}
            ref={setupRef}
            onKeyDown={(event) => {
              if (event.key === "Escape" && setupOpen) {
                setSetupOpen(false);
                setupRef.current?.querySelector("button")?.focus();
              }
            }}
          >
            <button
              type="button"
              className={navLinkClass(current === "setup")}
              aria-current={current === "setup" ? "true" : undefined}
              aria-haspopup="true"
              aria-expanded={setupOpen}
              onClick={() => {
                setSetupOpen(!setupOpen);
                setOpen(false);
              }}
            >
              Setup guide
              <svg
                className={styles.chev}
                width="10"
                height="10"
                viewBox="0 0 10 10"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
                strokeLinejoin="round"
                aria-hidden="true"
              >
                <path d="M2 3.5 5 6.5 8 3.5" />
              </svg>
            </button>
            {setupOpen && (
              <div className={styles.panel}>
                {SETUP_GUIDES.map((group) => (
                  <div key={group.label}>
                    <div className={styles.panelGroup}>{group.label}</div>
                    {group.items.map((item) => (
                      <a
                        key={item.href}
                        href={siteHref(item.href)}
                        onClick={() => setSetupOpen(false)}
                      >
                        {item.label}
                      </a>
                    ))}
                  </div>
                ))}
              </div>
            )}
          </div>
          <a
            href={siteHref("/docs")}
            aria-current={current === "docs" ? "true" : undefined}
            className={navLinkClass(current === "docs")}
          >
            Docs
          </a>
          <a
            href={siteHref("/blog")}
            aria-current={current === "blog" ? "true" : undefined}
            className={navLinkClass(current === "blog")}
          >
            Blog
          </a>
          <a
            href={siteHref("/pricing")}
            aria-current={current === "pricing" ? "true" : undefined}
            className={navLinkClass(current === "pricing")}
          >
            Pricing
          </a>
          {/* Only in the phone menu. The desktop bar keeps Contact in the footer. */}
          <a
            href={siteHref("/contact")}
            aria-current={current === "contact" ? "true" : undefined}
            className={`${navLinkClass(current === "contact")} ${styles.menuOnly}`}
          >
            Contact
          </a>
          {!accountAction && !loggedIn && (
            <a
              className={`${styles.navLink} ${styles.menuSignIn}`}
              href={siteHref("/signin")}
            >
              Sign in
            </a>
          )}
        </nav>
        <div className={styles.navActions}>
          <div className={styles.themeButton}>{themeControl}</div>
          {accountAction ? (
            <AnswerButton
              className={styles.primary}
              href={siteHref(accountAction.href)}
            >
              {accountAction.label}
            </AnswerButton>
          ) : loggedIn ? (
            <AnswerButton
              className={styles.primary}
              href={siteHref("/console")}
            >
              Console
            </AnswerButton>
          ) : (
            <>
              <a className={styles.account} href={siteHref("/signin")}>
                Sign in
              </a>
              <AnswerButton
                className={styles.primary}
                href={siteHref("/signup")}
              >
                Get started
              </AnswerButton>
            </>
          )}
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
              setSetupOpen(false);
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
