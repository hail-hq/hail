"use client";

import { useEffect, useId, useRef, type ReactNode } from "react";
import { AnswerButton } from "./AnswerButton";
const HEADER_COPY = {
  en: {
    home: "/",
    pricing: "/pricing",
    homeLabel: "hail home",
    navLabel: "main navigation",
    product: "Product",
    compare: "Compare and estimate",
    setup: "Setup guide",
    startHere: "Start here",
    templates: "Agent templates",
    phoneEmail: "Phone and email",
    docs: "Docs",
    blog: "Blog",
    pricingLabel: "Pricing",
    contact: "Contact",
    signIn: "Sign in",
    console: "Console",
    getStarted: "Get started",
    openMenu: "Open navigation",
    closeMenu: "Close navigation",
  },
} as const;
const chromeLabel = (_lang: Lang, label: string) => label;
type Lang = "en";
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

type MenuGroup = { label: string; items: { label: string; href: string }[] };

// Group headings are dictionary keys. Item labels are English and go through `chromeLabel`.
const RESOURCES: (MenuGroup & { label: "product" | "compare" })[] = [
  {
    label: "product",
    items: [
      { label: "Voice API", href: "/voice" },
      { label: "SMS API", href: "/sms" },
      { label: "Email API", href: "/email" },
      { label: "Integrations", href: "/integrations" },
      { label: "MCP connections", href: "/mcp" },
    ],
  },
  {
    label: "compare",
    items: [
      { label: "Compare platforms", href: "/compare" },
      { label: "Free tools", href: "/tools" },
      { label: "AI model pricing", href: "/costs" },
    ],
  },
];

const SETUP_GUIDES: (MenuGroup & {
  label: "startHere" | "templates" | "phoneEmail";
})[] = [
  {
    label: "startHere",
    items: [{ label: "Create an AI agent", href: "/create-ai-agent" }],
  },
  {
    label: "templates",
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
    label: "phoneEmail",
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
  lang = "en",
}: {
  active?: SiteHeaderActive;
  loggedIn?: boolean;
  themeControl: ReactNode;
  origin?: string;
  accountAction?: { href: string; label: string };
  lang?: Lang;
}) {
  const t = HEADER_COPY[lang];
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
        <a
          className={styles.logo}
          href={siteHref(t.home)}
          aria-label={t.homeLabel}
        >
          <Mascot size={26} />
          hail
        </a>
        <nav
          id={navId}
          className={`${styles.nav} ${mobileOpen ? styles.navOpen : ""}`}
          aria-label={t.navLabel}
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
              {t.product}
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
                    <div className={styles.panelGroup}>{t[group.label]}</div>
                    {group.items.map((item) => (
                      <a
                        key={item.href}
                        href={siteHref(item.href)}
                        onClick={() => setOpen(false)}
                      >
                        {chromeLabel(lang, item.label)}
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
              {t.setup}
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
                    <div className={styles.panelGroup}>{t[group.label]}</div>
                    {group.items.map((item) => (
                      <a
                        key={item.href}
                        href={siteHref(item.href)}
                        onClick={() => setSetupOpen(false)}
                      >
                        {chromeLabel(lang, item.label)}
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
            {t.docs}
          </a>
          <a
            href={siteHref("/blog")}
            aria-current={current === "blog" ? "true" : undefined}
            className={navLinkClass(current === "blog")}
          >
            {t.blog}
          </a>
          <a
            href={siteHref(t.pricing)}
            aria-current={current === "pricing" ? "true" : undefined}
            className={navLinkClass(current === "pricing")}
          >
            {t.pricingLabel}
          </a>
          {/* Only in the phone menu. The desktop bar keeps Contact in the footer. */}
          <a
            href={siteHref("/contact")}
            aria-current={current === "contact" ? "true" : undefined}
            className={`${navLinkClass(current === "contact")} ${styles.menuOnly}`}
          >
            {t.contact}
          </a>
          {!accountAction && !loggedIn && (
            <a
              className={`${styles.navLink} ${styles.menuSignIn}`}
              href={siteHref("/signin")}
            >
              {t.signIn}
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
              {t.console}
            </AnswerButton>
          ) : (
            <>
              <a className={styles.account} href={siteHref("/signin")}>
                {t.signIn}
              </a>
              <AnswerButton
                className={styles.primary}
                href={siteHref("/signup")}
              >
                {t.getStarted}
              </AnswerButton>
            </>
          )}
          <button
            ref={menuButton}
            type="button"
            className={`${styles.menuToggle} ${mobileOpen ? styles.menuOpen : ""}`}
            aria-label={mobileOpen ? t.closeMenu : t.openMenu}
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
