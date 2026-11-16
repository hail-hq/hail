import { compareHref } from "@/lib/url";
import {
  comparisonKey,
  type ComparisonCategory,
  type ComparisonModel,
} from "@/lib/comparison-key";

export function CompareLink({
  model,
  category,
}: {
  model: ComparisonModel;
  category: ComparisonCategory;
}) {
  return (
    <a
      href={compareHref([comparisonKey(category, model)])}
      className="compare-link"
      aria-label={`Add ${model.display_name} to comparison`}
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
      <span>Compare</span>
    </a>
  );
}
