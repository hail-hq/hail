"use client";

import {
  type ComponentProps,
  type MouseEvent,
  type PointerEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
} from "react";
import s from "./AnswerButton.module.css";

/** Keep in step with `--slide` in the CSS file. */
const SLIDE_MS = 900;
/** The tease holds the knob near the end for this long. Then it slides back. */
const TEASE_HOLD_MS = 1800;
/** Time from page load to the first tease. */
const INTRO_DELAY_MS = 1200;
/** Past this part of the track, letting go answers. Before it, the knob slides back. */
const ANSWER_AT = 0.6;
/** At this part of the track the link opens, with no need to let go. */
const END_AT = 0.98;
/** A pointer that moves less than this is a click or a tap. */
const TAP_PX = 8;
/** The spinner stops after this time, so a failed page load does not leave it on forever. */
const LOADING_LIMIT_MS = 15000;

const SPINNER_DOTS = 8;

/** Handset glyph (Material Icons "call", Apache 2.0). Same glyph as the video end card. */
const Handset = () => (
  <svg
    className={s.phone}
    width="24"
    height="24"
    viewBox="0 0 24 24"
    aria-hidden="true"
  >
    <path
      fill="currentColor"
      d="M6.62 10.79c1.44 2.83 3.76 5.14 6.59 6.59l2.2-2.2c.27-.27.67-.36 1.02-.24 1.12.37 2.33.57 3.57.57.55 0 1 .45 1 1V20c0 .55-.45 1-1 1-9.39 0-17-7.61-17-17 0-.55.45-1 1-1h3.5c.55 0 1 .45 1 1 0 1.25.2 2.45.57 3.57.11.35.03.74-.25 1.02l-2.2 2.2z"
    />
  </svg>
);

/** A ring of dots. Each dot is brighter than the last, and the ring turns one dot at a time. */
const Spinner = () => (
  <svg
    className={s.spinner}
    width="26"
    height="26"
    viewBox="0 0 24 24"
    aria-hidden="true"
  >
    {Array.from({ length: SPINNER_DOTS }, (_, dot) => (
      <circle
        key={dot}
        cx="12"
        cy="3.5"
        r="2.1"
        fill="currentColor"
        opacity={(dot + 1) / SPINNER_DOTS}
        transform={`rotate(${dot * (360 / SPINNER_DOTS)} 12 12)`}
      />
    ))}
  </svg>
);

type Props = Omit<ComponentProps<"a">, "children" | "className"> & {
  children: ReactNode;
  className?: string;
  /** `ink` is for a dark panel. */
  tone?: "paper" | "ink";
  /** Fill the width of the parent, up to the limit in the CSS file. Phones always do this. */
  block?: boolean;
  /** Play the tease once on page load. Use it once on a page, on the first call to action. */
  intro?: boolean;
};

type Gesture = {
  pointer: number;
  startX: number;
  start: number;
  travel: number;
  moved: boolean;
  progress: number;
};

const clamp = (value: number) => Math.min(1, Math.max(0, value));

/**
 * The slide-to-answer link from the video end card.
 * A click, a tap, a key press and a full slide all open the link, and each one shows the spinner until the next page.
 * With a mouse, hover plays a tease: the knob slides most of the way, waits, and slides back.
 * The knob never reaches the end by itself, because the end opens the link.
 * It stays a real link, so a keyboard and a click with no script both work.
 */
export function AnswerButton({
  children,
  className,
  tone = "paper",
  block = false,
  intro = false,
  onClick,
  ...link
}: Props) {
  const track = useRef<HTMLAnchorElement>(null);
  const knob = useRef<HTMLSpanElement>(null);
  const gesture = useRef<Gesture | null>(null);
  const dragged = useRef(false);
  const loading = useRef(false);
  const teaseTimer = useRef<number | undefined>(undefined);
  const loadingTimer = useRef<number | undefined>(undefined);
  const names = [s.track, block && s.block, className]
    .filter(Boolean)
    .join(" ");

  const setProgress = (progress: number) => {
    const node = track.current;
    if (!node) return;
    node.style.setProperty("--drag", String(progress));
    node.toggleAttribute("data-on", progress >= ANSWER_AT);
  };

  const stopTease = useCallback(() => {
    window.clearTimeout(teaseTimer.current);
    track.current?.removeAttribute("data-tease");
  }, []);

  const tease = useCallback(() => {
    if (
      gesture.current ||
      loading.current ||
      window.matchMedia?.("(prefers-reduced-motion: reduce)").matches
    )
      return;
    window.clearTimeout(teaseTimer.current);
    track.current?.setAttribute("data-tease", "");
    teaseTimer.current = window.setTimeout(stopTease, SLIDE_MS + TEASE_HOLD_MS);
  }, [stopTease]);

  const reset = useCallback(() => {
    stopTease();
    window.clearTimeout(loadingTimer.current);
    loading.current = false;
    const node = track.current;
    for (const name of [
      "data-dragging",
      "data-on",
      "data-loading",
      "aria-busy",
    ])
      node?.removeAttribute(name);
    node?.style.removeProperty("--drag");
  }, [stopTease]);

  const showLoading = () => {
    const node = track.current;
    if (!node) return;
    stopTease();
    loading.current = true;
    node.removeAttribute("data-dragging");
    node.setAttribute("data-loading", "");
    node.setAttribute("aria-busy", "true");
    setProgress(1);
    loadingTimer.current = window.setTimeout(reset, LOADING_LIMIT_MS);
  };

  const onPointerDown = (event: PointerEvent<HTMLAnchorElement>) => {
    // A browser often sends no click after a drag, so the last drag ends here.
    dragged.current = false;
    if (event.button !== 0 || loading.current) return;
    const node = event.currentTarget;
    const travel = node.offsetWidth - node.offsetHeight;
    // The tease can have the knob anywhere, so the drag starts from where the knob is now.
    const inset = (node.offsetHeight - (knob.current?.offsetHeight ?? 0)) / 2;
    const left =
      (knob.current?.getBoundingClientRect().left ?? 0) -
      node.getBoundingClientRect().left;
    const start = clamp((left - inset) / Math.max(1, travel));
    // Hold the knob where it is, also while it slides back after the tease.
    setProgress(start);
    stopTease();
    gesture.current = {
      pointer: event.pointerId,
      startX: event.clientX,
      start,
      travel,
      moved: false,
      progress: start,
    };
    node.setPointerCapture?.(event.pointerId);
  };

  const onPointerMove = (event: PointerEvent<HTMLAnchorElement>) => {
    const now = gesture.current;
    if (!now || event.pointerId !== now.pointer) return;
    const distance = event.clientX - now.startX;
    if (!now.moved && Math.abs(distance) < TAP_PX) return;
    now.moved = true;
    now.progress = clamp(now.start + distance / Math.max(1, now.travel));
    event.currentTarget.setAttribute("data-dragging", "");
    setProgress(now.progress);
    if (now.progress >= END_AT) {
      gesture.current = null;
      event.currentTarget.click();
    }
  };

  const onPointerUp = (event: PointerEvent<HTMLAnchorElement>) => {
    const now = gesture.current;
    gesture.current = null;
    if (!now?.moved) return;
    if (now.progress >= ANSWER_AT) {
      event.currentTarget.click();
      return;
    }
    dragged.current = true;
    reset();
  };

  // The browser takes the gesture when the finger scrolls the page.
  const onPointerCancel = () => {
    gesture.current = null;
    reset();
  };

  // Control-click on a Mac opens a menu and sends no click, so the press must not hold the knob.
  const onContextMenu = () => {
    if (loading.current) return;
    gesture.current = null;
    reset();
  };

  const onLinkClick = (event: MouseEvent<HTMLAnchorElement>) => {
    // The click that the browser sends after a drag. The drag has already decided.
    if (dragged.current) {
      dragged.current = false;
      event.preventDefault();
      return;
    }
    if (loading.current) {
      event.preventDefault();
      return;
    }
    onClick?.(event);
    const newTab =
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey ||
      link.target === "_blank";
    if (event.defaultPrevented || newTab) reset();
    else showLoading();
  };

  useEffect(() => {
    if (intro) teaseTimer.current = window.setTimeout(tease, INTRO_DELAY_MS);
    // The back button can restore this page with the knob still across.
    // `persisted` is false on a normal load, and that event must not stop the tease.
    const onPageShow = (event: PageTransitionEvent) => {
      if (event.persisted) reset();
    };
    window.addEventListener("pageshow", onPageShow);
    return () => {
      window.removeEventListener("pageshow", onPageShow);
      window.clearTimeout(teaseTimer.current);
      window.clearTimeout(loadingTimer.current);
    };
  }, [intro, reset, tease]);

  return (
    <a
      {...link}
      ref={track}
      className={names}
      data-tone={tone}
      draggable={false}
      onClick={onLinkClick}
      onPointerEnter={(event) => {
        if (event.pointerType === "mouse") tease();
      }}
      onPointerLeave={(event) => {
        if (event.pointerType === "mouse" && !gesture.current) stopTease();
      }}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerCancel}
      onContextMenu={onContextMenu}
    >
      <span className={s.fill} aria-hidden="true" />
      <span ref={knob} className={s.knob}>
        <Handset />
        <Spinner />
      </span>
      <span className={s.label}>{children}</span>
    </a>
  );
}
