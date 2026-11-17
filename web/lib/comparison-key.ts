import type { CommonFields } from "./types";

export type ComparisonCategory = "llm" | "stt" | "tts";
export type ComparisonModel = Pick<
  CommonFields,
  "provider" | "model_id" | "display_name"
> & {
  streaming?: boolean;
  realtime?: boolean;
};

/** Provider IDs can name separate batch and realtime products. Keep those products distinct. */
export function comparisonKey(
  category: ComparisonCategory,
  model: ComparisonModel,
): string {
  const mode =
    category === "stt"
      ? `${model.streaming ? "streaming" : "batch"}-${model.realtime ? "realtime" : "standard"}`
      : "default";
  return [category, model.provider, model.model_id, mode]
    .map(encodeURIComponent)
    .join(":");
}
