import { url } from '@/lib/url';

export function CompareLink({ modelId }: { modelId: string }) {
  return (
    <a
      href={url(`/costs/compare?m=${modelId}`)}
      className="compare-link"
      aria-label={`Add ${modelId} to comparison`}
      title="Add to compare"
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
    </a>
  );
}
