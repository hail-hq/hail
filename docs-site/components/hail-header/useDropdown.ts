"use client";

import { useEffect, useRef, useState } from "react";

/**
 * Open/close state for a popover menu. While open it closes on:
 * - a click outside (a drag that starts inside and ends outside does not count),
 * - a right or middle press, or a press on a disabled control, outside,
 * - a context menu outside (right-click, or Control+click on a Mac),
 * - Escape.
 * Attach `ref` to the menu's root element and drive the trigger button with
 * `open` / `setOpen`. Document listeners are bound only while open.
 */
export function useDropdown(initialOpen = false) {
  const [open, setOpen] = useState(initialOpen);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    let pressedInside = false;
    const outside = (e: Event) => !ref.current?.contains(e.target as Node);
    function onPointerDown(e: PointerEvent) {
      const inside = !outside(e);
      // Only a primary press is followed by a click that we must ignore.
      pressedInside = e.button === 0 && inside;
      // These presses send no click, so close now. Nothing under them can move.
      const control = (e.target as Element | null)?.closest?.(
        "button, input, select, textarea, option, optgroup",
      );
      if (!inside && (e.button !== 0 || control?.matches(":disabled")))
        setOpen(false);
    }
    function onDocClick(e: MouseEvent) {
      // A drag that starts in the menu (text selection) clicks the common
      // ancestor outside it. Keep the menu open for that.
      if (outside(e) && !pressedInside) setOpen(false);
      pressedInside = false;
    }
    function onKey(e: KeyboardEvent) {
      // A key press starts a new action: a press inside that sent no click
      // (right-click, cancelled touch scroll) must not keep the menu open.
      pressedInside = false;
      if (e.key === "Escape") setOpen(false);
    }
    // A press that ended without a click, such as a touch scroll.
    function onPointerCancel() {
      pressedInside = false;
    }
    // Opening a context menu outside closes the menu. On a Mac, Control+click
    // reports the primary button and sends no click.
    function onContextMenu(e: MouseEvent) {
      if (outside(e)) setOpen(false);
      pressedInside = false;
    }
    // Close a primary press on its click, not on pointerdown: the target must
    // get its click before a collapsing menu moves it. Capture also sees
    // events stopped by controls such as the console's touch timestamps.
    document.addEventListener("pointerdown", onPointerDown, true);
    document.addEventListener("click", onDocClick, true);
    document.addEventListener("keydown", onKey, true);
    document.addEventListener("pointercancel", onPointerCancel, true);
    document.addEventListener("contextmenu", onContextMenu, true);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown, true);
      document.removeEventListener("click", onDocClick, true);
      document.removeEventListener("keydown", onKey, true);
      document.removeEventListener("pointercancel", onPointerCancel, true);
      document.removeEventListener("contextmenu", onContextMenu, true);
    };
  }, [open]);

  return { open, setOpen, ref };
}
