import Link from 'next/link';
import { url } from '@/lib/url';

/**
 * Tiny "+ compare" chip on each costs-table row. Clicking starts a compare
 * URL with this one model preselected; the compare page's picker handles
 * adding more. Uses <Link> (same-zone navigation inside docs).
 */
export function CompareLink({ modelId }: { modelId: string }) {
  return (
    <Link
      href={url(`/costs/compare?m=${encodeURIComponent(modelId)}`) as never}
      className="compare-link"
      aria-label={`Add ${modelId} to comparison`}
      title="Add to compare"
      prefetch={false}
    >
      <svg
        viewBox="0 0 16 16"
        width="10"
        height="10"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        aria-hidden="true"
      >
        <path d="M8 3v10M3 8h10" />
      </svg>
      <span>compare</span>
    </Link>
  );
}
