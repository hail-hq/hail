import { CompareLink } from './compare-link';
import { CopyableCode } from './copyable-code';

export function ModelIdCell({ modelId }: { modelId: string }) {
  return (
    <div className="model-id-row">
      <CopyableCode value={modelId} />
      <CompareLink modelId={modelId} />
    </div>
  );
}
