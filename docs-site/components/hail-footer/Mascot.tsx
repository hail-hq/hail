import styles from "./Mascot.module.css";

/** The Hail bird as inline SVG, so CSS can blink its eye and nod it when the
 * link around it is hovered. One component for the header, the footer, and
 * every page; the sync scripts copy it to the costs and docs apps. Mirrors
 * the `bird.svg` brand file; keep the two in step. Decorative:
 * hidden from assistive technology unless `title` is given. */
export function Mascot({
  size = 32,
  className,
  title,
}: {
  size?: number;
  className?: string;
  title?: string;
}) {
  return (
    <svg
      className={[styles.mascot, className].filter(Boolean).join(" ")}
      viewBox="0 0 160 160"
      width={size}
      height={size}
      fill="none"
      aria-hidden={title ? undefined : true}
      role={title ? "img" : undefined}
      focusable="false"
    >
      {title ? <title>{title}</title> : null}
      <path
        d="M31 118C17 100 23 79 38 65C41 37 57 20 82 20C112 20 128 44 125 76C123 100 108 124 79 130C61 134 41 130 31 118Z"
        fill="#5284FF"
      />
      <path d="M120 55L148 65L123 79Z" fill="#F6B858" />
      <path
        data-mascot-part="wing"
        d="M53 82C45 91 49 105 64 108C79 109 87 98 85 85"
        stroke="#183A8F"
        strokeWidth="6"
        strokeLinecap="round"
      />
      <g className={styles.eye}>
        <ellipse cx="103" cy="52" rx="10" ry="13" fill="#FFFDF6" />
        <ellipse cx="107" cy="53" rx="4" ry="6" fill="#172447" />
      </g>
      <path
        d="M68 129L64 144M89 130L93 143"
        stroke="#172447"
        strokeWidth="5"
        strokeLinecap="round"
      />
      <g data-mascot-part="letter">
        <rect
          x="36"
          y="100"
          width="51"
          height="34"
          rx="5"
          transform="rotate(-12 36 100)"
          fill="#FFFDF6"
        />
        <path
          d="M40 104L65 115L87 94"
          stroke="#5284FF"
          strokeWidth="3"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </g>
    </svg>
  );
}
