"use client";

import { useEffect, useRef } from "react";

/** Same width as the footer's phone layout in footer.css. */
const PHONE = "(max-width: 900px)";

/**
 * The footer renders each link group once, as a closed `<details>`. Closed is right for phones: no layout jump.
 * On wide screens footer.css shows every group anyway (`::details-content`).
 * This component covers browsers that lack `::details-content`: on wide screens it opens the groups.
 * On phones it makes the accordion exclusive, so opening one group closes the others.
 * Put it inside the element that holds the groups.
 */
export function FooterAccordion() {
  const marker = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    const host = marker.current?.parentElement;
    // The groups stay closed when this browser cannot match a width. Phones then still get the accordion.
    if (!host || typeof window.matchMedia !== "function") return;
    const query = window.matchMedia(PHONE);
    const apply = () => {
      for (const details of host.querySelectorAll<HTMLDetailsElement>("details")) {
        if (query.matches) details.setAttribute("name", "hail-footer-groups");
        else { details.removeAttribute("name"); details.open = true; }
      }
    };
    apply();
    query.addEventListener("change", apply);
    return () => query.removeEventListener("change", apply);
  }, []);
  return <span ref={marker} hidden />;
}
