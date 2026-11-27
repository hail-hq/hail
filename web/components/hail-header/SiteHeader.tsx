"use client";

import { useCallback, useEffect, useId, useRef, type ReactNode } from "react";
import { AnswerButton } from "./AnswerButton";
import { useDropdown } from "./useDropdown";
import { Mascot } from "./Mascot";
import styles from "./SiteHeader.module.css";

export type SiteHeaderActive =
  | "product"
  | "use-cases"
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

const SOLUTIONS = [
  {
    label: "Answer and route",
    items: [
      { label: "AI receptionist", href: "/receptionist" },
      { label: "Answer calls for trades", href: "/trades" },
      { label: "AI phone agent", href: "/phone-agent" },
      { label: "Call routing", href: "/call-routing" },
      { label: "Call center overflow", href: "/call-center" },
    ],
  },
  {
    label: "Follow up",
    items: [
      { label: "Bookings and support tickets", href: "/follow-up" },
      { label: "AI secretary", href: "/secretary" },
      { label: "Outbound callbacks", href: "/outbound" },
    ],
  },
  {
    label: "Set up and estimate",
    items: [
      { label: "Keep your business number", href: "/keep-number" },
      { label: "Test before going live", href: "/test-calls" },
      { label: "Estimate answering costs", href: "/answering-cost" },
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

function usesDesktopNav() {
  return window.matchMedia?.("(width > 900px)").matches;
}

function hasKeyboardFocusedLink(root: HTMLDivElement | null) {
  const focused = document.activeElement;
  return focused?.matches("a:focus-visible") && root?.contains(focused);
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
  const product = useDropdown(false, { escapeHandledInside: true });
  const solutions = useDropdown(false, { escapeHandledInside: true });
  const setup = useDropdown(false, { escapeHandledInside: true });
  const menus = [
    { section: "product", label: "Product", groups: RESOURCES, menu: product },
    {
      section: "use-cases",
      label: "Solutions",
      groups: SOLUTIONS,
      menu: solutions,
    },
    {
      section: "setup",
      label: "Setup guide",
      groups: SETUP_GUIDES,
      menu: setup,
    },
  ] as const;
  const hoveredMenu = useRef<SiteHeaderActive>(null);
  const {
    open: mobileOpen,
    setOpen: setMobileOpen,
    ref: mobileRef,
  } = useDropdown(false, { escapeHandledInside: true });
  const menuButton = useRef<HTMLButtonElement>(null);
  const navId = useId();

  const { setOpen: setProductOpen } = product;
  const { setOpen: setSolutionsOpen } = solutions;
  const { setOpen: setSetupOpen } = setup;
  const openMenu = useCallback(
    (section: SiteHeaderActive) => {
      setProductOpen(section === "product");
      setSolutionsOpen(section === "use-cases");
      setSetupOpen(section === "setup");
    },
    [setProductOpen, setSolutionsOpen, setSetupOpen],
  );

  useEffect(() => {
    const media = window.matchMedia?.("(max-width: 900px)");
    if (!mobileOpen || !media?.matches) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const closeOnDesktop = () => {
      if (media.matches) return;
      const focusedDropdown = document.activeElement?.closest(
        `.${styles.dropdown}`,
      );
      focusedDropdown?.querySelector("button")?.focus();
      hoveredMenu.current = null;
      openMenu(null);
      setMobileOpen(false);
    };
    media.addEventListener("change", closeOnDesktop);
    return () => {
      document.body.style.overflow = previous;
      media.removeEventListener("change", closeOnDesktop);
    };
  }, [mobileOpen, openMenu, setMobileOpen]);

  return (
    <header className={styles.header} data-public-header>
      <div
        className={styles.shell}
        ref={mobileRef}
        onKeyDown={(event) => {
          if (event.key === "Escape" && mobileOpen) {
            setMobileOpen(false);
            openMenu(null);
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
          hail
        </a>
        <nav
          id={navId}
          className={`${styles.nav} ${mobileOpen ? styles.navOpen : ""}`}
          aria-label="main navigation"
          onClick={(event) => {
            const link = (event.target as Element).closest<HTMLAnchorElement>(
              "a[href]",
            );
            if (
              !link ||
              event.button !== 0 ||
              event.metaKey ||
              event.ctrlKey ||
              event.shiftKey ||
              event.altKey
            )
              return;
            if (link.target && link.target !== "_self") return;
            setMobileOpen(false);
            openMenu(null);
          }}
        >
          {menus.map(
            ({ section, label, groups, menu: { open, setOpen, ref } }) => (
              <div
                key={section}
                className={styles.dropdown}
                ref={ref}
                onPointerEnter={(event) => {
                  if (event.pointerType !== "mouse" || !usesDesktopNav())
                    return;
                  hoveredMenu.current = section;
                  if (
                    menus.some(
                      (item) =>
                        item.section !== section &&
                        hasKeyboardFocusedLink(item.menu.ref.current),
                    )
                  )
                    return;
                  openMenu(section);
                }}
                onPointerLeave={(event) => {
                  if (
                    event.pointerType !== "mouse" ||
                    hoveredMenu.current !== section
                  )
                    return;
                  hoveredMenu.current = null;
                  if (!hasKeyboardFocusedLink(ref.current)) setOpen(false);
                }}
                onBlur={(event) => {
                  // Phone panels must stay in place until the next click lands.
                  if (!usesDesktopNav()) return;
                  if (
                    open &&
                    event.relatedTarget &&
                    !event.currentTarget.contains(event.relatedTarget) &&
                    hoveredMenu.current !== section
                  ) {
                    openMenu(hoveredMenu.current);
                  }
                }}
                onKeyDown={(event) => {
                  if (event.key === "Escape" && open) {
                    setOpen(false);
                    ref.current?.querySelector("button")?.focus();
                  }
                }}
              >
                <button
                  type="button"
                  className={navLinkClass(current === section)}
                  aria-current={current === section ? "true" : undefined}
                  aria-haspopup={section === "use-cases" ? undefined : "true"}
                  aria-expanded={open}
                  onClick={(event) => {
                    const pointerType = (event.nativeEvent as PointerEvent)
                      .pointerType;
                    const mouseClick =
                      event.detail > 0 &&
                      (!pointerType || pointerType === "mouse");
                    openMenu(
                      (mouseClick && hoveredMenu.current === section) || !open
                        ? section
                        : null,
                    );
                  }}
                >
                  {label}
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
                  <div
                    className={
                      section === "use-cases"
                        ? `${styles.panelPosition} ${styles.solutionsPosition}`
                        : styles.panelPosition
                    }
                  >
                    <div
                      className={
                        section === "use-cases"
                          ? `${styles.panel} ${styles.solutionsPanel}`
                          : styles.panel
                      }
                    >
                      {groups.map((group) => (
                        <div key={group.label}>
                          <div className={styles.panelGroup}>{group.label}</div>
                          {group.items.map((item) => (
                            <a key={item.href} href={siteHref(item.href)}>
                              {item.label}
                            </a>
                          ))}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            ),
          )}
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
              openMenu(null);
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
