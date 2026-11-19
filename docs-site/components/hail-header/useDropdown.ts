"use client";

import { useEffect, useRef, useState } from "react";

/**
 * Open/close state for a popover menu: closes on outside-click or Escape while
 * open. Attach `ref` to the menu's root element and drive the trigger button
 * with `open` / `setOpen`. Document listeners are bound only while open.
 */
export function useDropdown(initialOpen = false) {
  const [open, setOpen] = useState(initialOpen);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onDocClick(e: MouseEvent) {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    // The release target is fixed by click dispatch. Capture also sees clicks
    // stopped by controls such as the console's touch timestamps.
    document.addEventListener("click", onDocClick, true);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("click", onDocClick, true);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return { open, setOpen, ref };
}
