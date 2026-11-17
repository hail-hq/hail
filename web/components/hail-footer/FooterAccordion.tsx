"use client";

import { useEffect, useRef } from "react";

/** Same width as the footer's phone layout in footer.css. */
const PHONE = "(max-width: 900px)";

/**
 * The footer renders each link group once, as an open `<details>`. Without JavaScript every link shows at every width.
 * On phones this closes the groups to make an accordion. On wider screens it keeps them open and out of the tab order.
 * Put it inside the element that holds the groups.
 */
export function FooterAccordion() {
  const marker = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    const host = marker.current?.parentElement;
    // The groups stay open when this browser cannot match a width. Nothing else depends on it.
    if (!host || typeof window.matchMedia !== "function") return;
    const query = window.matchMedia(PHONE);
    const apply = () => {
      for (const details of host.querySelectorAll<HTMLDetailsElement>("details")) {
        details.open = !query.matches;
        const summary = details.querySelector("summary");
        if (summary) summary.tabIndex = query.matches ? 0 : -1;
      }
    };
    apply();
    query.addEventListener("change", apply);
    return () => query.removeEventListener("change", apply);
  }, []);
  return <span ref={marker} hidden />;
}
