import { CompareLink } from "./compare-link";
import { CopyableCode } from "./copyable-code";
import type { ComparisonCategory, ComparisonModel } from "@/lib/comparison-key";

export function ModelIdCell({
  model,
  category,
}: {
  model: ComparisonModel;
  category: ComparisonCategory;
}) {
  return (
    <div className="model-id-row">
      <CopyableCode value={model.model_id} />
      <CompareLink model={model} category={category} />
    </div>
  );
}
