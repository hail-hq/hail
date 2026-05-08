export type Modality = 'text' | 'image' | 'audio' | 'video';

export type CommonFields = {
  provider: string;
  provider_url?: string;
  model_id: string;
  display_name: string;
  last_verified: string;
  verified_by: string;
  source_url: string;
  notes?: string;
};

export type LLMRow = CommonFields & {
  model_family?: string;
  release_date?: string;
  knowledge_cutoff?: string;
  context_window: number;
  max_output_tokens: number;
  input_per_mtok_usd: number;
  output_per_mtok_usd: number;
  cached_input_per_mtok_usd?: number;
  modalities: { input: Modality[]; output: Modality[] };
  tool_use?: boolean;
  structured_output?: boolean;
};

export type STTRow = CommonFields & {
  price_per_minute_usd: number;
  price_per_minute_batch_usd?: number;
  languages: string[] | string;
  streaming: boolean;
  realtime?: boolean;
  diarization?: 'included' | 'extra-cost' | 'unsupported';
  wer_benchmark?: { dataset: string; wer_pct: number; source_url?: string };
  time_to_first_word_ms?: number;
};

export type TTSRow = CommonFields & {
  price_per_1m_chars_usd: number;
  voice_quality: 'standard' | 'neural' | 'cloned';
  voice_count?: number;
  languages: string[] | string;
  ssml_support?: boolean;
  voice_cloning?: boolean | { price_usd: number; unit: 'per-clone' | 'monthly' | 'per-1m-chars' };
  output_formats?: string[];
  time_to_first_byte_ms?: number;
};

export type CostsFile<T> = {
  version: 1;
  updated: string;
  license: 'CC-BY-4.0';
  models: T[];
};
