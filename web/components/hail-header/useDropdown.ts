"use client";

import { useEffect, useRef, useState } from "react";

/**
 * iOS Safari sends no click for a tap on an element that cannot take one,
 * such as blank page area. These are the elements that can take one. An
 * element with `cursor: pointer` (inherited from an ancestor too) can as well.
 */
const CLICKABLE =
  'a[href], button, input, select, textarea, label, summary, [role="button"], [role="link"], ' +
  '[role="menuitem"], [role="option"], [role="tab"], [tabindex]:not([tabindex="-1"]), [onclick]';

/** A press held longer than this is a long press (text selection, callout), not a tap. */
const LONG_PRESS_MS = 500;
/** A finger that moves farther than this swiped; it did not tap. */
const TAP_SLOP_PX = 10;

/**
 * Only iOS and iPadOS Safari skip the click on blank area. Other touch
 * browsers send it, and the menu must wait for it there: closing first can
 * move a link under the finger before the click lands.
 */
function skipsBlankAreaClick() {
  const { userAgent, maxTouchPoints } = navigator;
  return (
    /iP(hone|ad|od)/.test(userAgent) ||
    (/Macintosh/.test(userAgent) && maxTouchPoints > 1)
  );
}

function canReceiveClick(target: EventTarget | null) {
  const el = target as Element | null;
  if (!el?.closest) return false;
  if (
    el.closest(CLICKABLE) !== null ||
    getComputedStyle(el).cursor === "pointer"
  )
    return true;
  // React sets `onclick` on every element with an `onClick` prop, so that
  // iOS sends it a click. The `[onclick]` selector sees only the attribute.
  // Stop below `<body>`: React also sets it there for a portal, and blank page
  // area still gets no click on iOS. Closing early there costs nothing.
  for (
    let node: Element | null = el;
    node && node !== el.ownerDocument.body;
    node = node.parentElement
  ) {
    if (typeof (node as HTMLElement).onclick === "function") return true;
  }
  return false;
}

/**
 * Open/close state for a popover menu. While open it closes on:
 * - a click outside (a drag that starts inside and ends outside does not count),
 * - on iOS and iPadOS, a touch or pen tap outside on something that cannot
 *   take a click (Safari sends none on blank area), when the finger lifts. Not
 *   for a scroll, a swipe, a long press, or a gesture with a second finger.
 *   Elements that get a click only through addEventListener look blank here,
 *   so the menu closes just before their click,
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
    // A primary touch or pen press that started outside: its pointer and time.
    let tap: { id: number; at: number; x: number; y: number } | null = null;
    const ios = skipsBlankAreaClick();
    const outside = (e: Event) => !ref.current?.contains(e.target as Node);
    function onPointerDown(e: PointerEvent) {
      const inside = !outside(e);
      // Only a primary press is followed by a click that we must ignore. A
      // second finger must not reset the press of the first one.
      const touch = e.pointerType === "touch" || e.pointerType === "pen";
      if (e.isPrimary || !touch) pressedInside = e.button === 0 && inside;
      // A second finger is not primary, so any press here also ends a tap in
      // progress: a gesture with two fingers is not a tap.
      tap =
        ios && e.button === 0 && !inside && e.isPrimary && touch
          ? { id: e.pointerId, at: e.timeStamp, x: e.clientX, y: e.clientY }
          : null;
      // These presses send no click, so close now. Nothing under them can move.
      const control = (e.target as Element | null)?.closest?.(
        "button, input, select, textarea, option, optgroup",
      );
      if (!inside && (e.button !== 0 || control?.matches(":disabled")))
        setOpen(false);
    }
    // A target that can take a click waits for it: onDocClick closes the menu
    // after the target gets its click, however late the click comes.
    function onPointerUp(e: PointerEvent) {
      if (!tap || e.pointerId !== tap.id) return;
      const quick = e.timeStamp - tap.at <= LONG_PRESS_MS;
      const still =
        Math.hypot(e.clientX - tap.x, e.clientY - tap.y) <= TAP_SLOP_PX;
      tap = null;
      if (quick && still && outside(e) && !canReceiveClick(e.target))
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
    function onPointerCancel(e: PointerEvent) {
      pressedInside = false;
      if (tap?.id === e.pointerId) tap = null;
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
    // A touch tap on something that cannot take a click closes on pointerup.
    document.addEventListener("pointerdown", onPointerDown, true);
    document.addEventListener("pointerup", onPointerUp, true);
    document.addEventListener("click", onDocClick, true);
    document.addEventListener("keydown", onKey, true);
    document.addEventListener("pointercancel", onPointerCancel, true);
    document.addEventListener("contextmenu", onContextMenu, true);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown, true);
      document.removeEventListener("pointerup", onPointerUp, true);
      document.removeEventListener("click", onDocClick, true);
      document.removeEventListener("keydown", onKey, true);
      document.removeEventListener("pointercancel", onPointerCancel, true);
      document.removeEventListener("contextmenu", onContextMenu, true);
    };
  }, [open]);

  return { open, setOpen, ref };
}
