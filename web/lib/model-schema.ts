import type { LLMRow, STTRow, TTSRow } from "./types";

type Model = LLMRow | STTRow | TTSRow;
type Category = "llm" | "stt" | "tts";

const categories = {
  llm: "Language model",
  stt: "Speech recognition model",
  tts: "Speech synthesis model",
};

// Only price components shown in the comparison table belong in its schema.
// These are provider usage rates, not a single purchase price or a Hail quote.
const rates = {
  llm: [
    ["input_per_mtok", "Input", "1 million input tokens"],
    ["output_per_mtok", "Output", "1 million output tokens"],
    ["cache_read_per_mtok", "Cached input", "1 million cached input tokens"],
  ],
  stt: [
    ["price_per_minute", "Transcription", "1 minute of audio"],
    [
      "price_per_minute_batch",
      "Batch transcription",
      "1 minute of batch audio",
    ],
  ],
  tts: [
    ["price_per_1m_chars", "Speech synthesis", "1 million characters"],
    ["price_per_1m_text_tokens", "Text input", "1 million input text tokens"],
    ["price_per_1m_audio_tokens", "Audio output", "1 million output audio tokens"],
  ],
} as const;

export function modelProduct(
  model: Model,
  category: Category,
  pageUrl: string,
) {
  if (
    !model.display_name.trim() ||
    !model.model_id.trim() ||
    !model.provider.trim()
  ) {
    throw new Error("A model product needs its name, model ID, and provider.");
  }
  const fields = model as unknown as Record<string, unknown>;
  const offers = model.deprecated_at
    ? []
    : rates[category].flatMap(([field, name, unit]) => {
        // Match rowPrice(): prefer USD and fall back to EUR when USD is absent.
        const currency = fields[`${field}_usd`] !== undefined ? "USD" : "EUR";
        const price = fields[`${field}_${currency.toLowerCase()}`];
        if (typeof price !== "string" || !/^\d+(?:\.\d+)?$/.test(price))
          return [];
        return [
          {
            "@type": "Offer",
            name,
            url: model.source_url,
            priceSpecification: {
              "@type": "UnitPriceSpecification",
              price,
              priceCurrency: currency,
              unitText: unit,
            },
          },
        ];
      });
  return {
    "@type": "Product",
    "@id": `${pageUrl}#model-${encodeURIComponent(model.model_id)}`,
    url: pageUrl,
    name: model.display_name,
    ...(!fields.api_endpoint ? { model: model.model_id } : {}),
    category: categories[category],
    brand: { "@type": "Brand", name: model.provider },
    description: `${model.display_name}: ${categories[category].toLowerCase()} from ${model.provider}.`,
    ...(offers.length ? { offers } : {}),
  };
}

export function modelComparisonSchema(input: {
  models: readonly [Model, Model];
  category: Category;
  path: string;
  origin: string;
}) {
  const url = new URL(input.path, input.origin).href;
  const products = input.models.map((model) =>
    modelProduct(model, input.category, url),
  );
  return {
    "@context": "https://schema.org",
    "@graph": [
      {
        "@type": "WebPage",
        "@id": url,
        url,
        name: products.map((product) => product.name).join(" vs "),
        mainEntity: products.map((product) => ({ "@id": product["@id"] })),
      },
      ...products,
    ],
  };
}
