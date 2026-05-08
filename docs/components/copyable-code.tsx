'use client';

import { useState, useCallback } from 'react';

async function copyToClipboard(text: string): Promise<boolean> {
  if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Fall through to legacy fallback below.
    }
  }
  if (typeof document === 'undefined') return false;
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', '');
  ta.style.position = 'fixed';
  ta.style.left = '-9999px';
  ta.style.top = '0';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  try {
    ta.focus();
    ta.select();
    return document.execCommand('copy');
  } catch {
    return false;
  } finally {
    document.body.removeChild(ta);
  }
}

export function CopyableCode({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);

  const onClick = useCallback(
    async (e: React.MouseEvent) => {
      e.preventDefault();
      e.stopPropagation();
      const ok = await copyToClipboard(value);
      if (ok) {
        setCopied(true);
        setTimeout(() => setCopied(false), 1400);
      }
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
