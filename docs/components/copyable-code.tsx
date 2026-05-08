'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

export function CopyableCode({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (timerRef.current !== null) clearTimeout(timerRef.current);
    };
  }, []);

  const onClick = useCallback(
    async (e: React.MouseEvent) => {
      e.preventDefault();
      e.stopPropagation();
      if (!navigator.clipboard?.writeText) return;
      try {
        await navigator.clipboard.writeText(value);
      } catch {
        return;
      }
      setCopied(true);
      if (timerRef.current !== null) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => setCopied(false), 1400);
    },
    [value],
  );

  return (
    <button
      type="button"
      onClick={onClick}
      className={`copy-chip${copied ? ' copy-chip--copied' : ''}`}
      title={copied ? 'Copied!' : 'Click to copy'}
      aria-label={copied ? `Copied ${value}` : `Copy model id ${value}`}
    >
      <span className="copy-chip-text">{value}</span>
      <span className="copy-chip-icon" aria-hidden="true">
        {copied ? (
          <svg viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="3 8.5 6.5 12 13 4.5" />
          </svg>
        ) : (
          <svg viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
            <rect x="5.5" y="5.5" width="8" height="8" rx="1" />
            <path d="M3 10.5V3.5A1 1 0 0 1 4 2.5h7" />
          </svg>
        )}
      </span>
    </button>
  );
}
