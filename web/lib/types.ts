export type Modality = 'text' | 'image' | 'audio' | 'video';
export type Confidence = 'high' | 'medium' | 'low';
export type VerificationMethod = 'manual-confirmed' | 'community-pr';
export type DeploymentOption =
  | 'native'
  | 'bedrock'
  | 'vertex'
  | 'azure'
  | 'together'
  | 'fireworks'
  | 'groq'
  | 'replicate'
  | 'dashscope'
  | 'openrouter';

export type Aggregator = 'openrouter';

export type CommonFields = {
  provider: string;
  provider_url?: string;
  model_id: string;
  display_name: string;
  last_verified: string;
  last_changed_at: string;
  verification_method: VerificationMethod;
  verified_by: string;
  source_url: string;
  notes?: string;
  confidence?: Confidence;
  deprecated_at?: string;
  replaced_by_model_id?: string;
  aliases?: string[];
  deployment_options?: DeploymentOption[];
  free_tier?: { requests_per_day?: number; tokens_per_day?: number };
};

export type LLMRow = CommonFields & {
  model_family?: string;
  release_date?: string;
  knowledge_cutoff?: string;
  context_window: number;
  max_output_tokens: number;
  input_per_mtok_usd: string;
  output_per_mtok_usd: string;
  audio_input_per_mtok_usd?: string;
  cache_read_per_mtok_usd?: string;
  cache_write_per_mtok_usd?: string;
  cache_storage_per_mtok_per_hour_usd?: string;
  per_request_usd?: string;
  per_search_usd?: string;
  batch_input_per_mtok_usd?: string;
  batch_output_per_mtok_usd?: string;
  pricing_tiers?: {
    threshold_tokens: number;
    input_per_mtok_usd: string;
    output_per_mtok_usd: string;
    cache_read_per_mtok_usd?: string;
  }[];
  modalities: { input: Modality[]; output: Modality[] };
  supports_tool_use?: boolean;
  structured_output?: boolean;
  supports_vision?: boolean;
  supports_audio_in?: boolean;
  supports_audio_out?: boolean;
  supports_pdf?: boolean;
  reasoning_tokens_billed?: boolean;
  latency_benchmark?: {
    source_url: string;
    measured_at: string;
    ttft_ms?: number;
    output_tps?: number;
    confidence?: Confidence;
  };
  aggregators?: Aggregator[];
};

export type STTRow = CommonFields & {
  price_per_minute_usd?: string;
  price_per_second_usd?: string;
  price_per_minute_batch_usd?: string;
  diarization_per_minute_usd?: string;
  pii_redaction_per_minute_usd?: string;
  languages: string[] | string;
  streaming: boolean;
  realtime?: boolean;
  realtime_latency_ms?: number;
  diarization?: 'included' | 'extra-cost' | 'unsupported';
  punctuation_included?: boolean;
  formatting_included?: boolean;
  min_billed_seconds?: number;
  max_audio_minutes_per_file?: number;
  concurrent_streams_included?: number;
  wer_benchmark?: {
    dataset: string;
    wer_pct: number;
    source_url?: string;
    confidence?: Confidence;
  };
  time_to_first_word_ms?: number;
  aggregators?: Aggregator[];
};

export type TTSRow = CommonFields & {
  price_per_1m_chars_usd?: string;
  price_per_second_usd?: string;
  voice_quality: 'standard' | 'neural' | 'cloned';
  voice_count?: number;
  voices_count?: number;
  voices_premium_count?: number;
  languages: string[] | string;
  ssml_supported?: boolean;
  emotion_control_supported?: boolean;
  streaming_supported?: boolean;
  voice_cloning?:
    | boolean
    | {
        price_usd: string;
        unit: 'per-clone' | 'monthly' | 'per-1m-chars';
        min_seconds?: number;
      };
  output_formats?: string[];
  sample_rates_hz?: number[];
  time_to_first_byte_ms?: number;
  min_billed_chars?: number;
  aggregators?: Aggregator[];
};

interface TelephonyProvenance {
  last_verified: string;
  last_changed_at: string;
  verification_method: VerificationMethod;
  verified_by: string;
  source_url: string;
  notes?: string;
}

export interface TelephonyNumberRow extends TelephonyProvenance {
  country_code: string;
  number_type: 'local' | 'mobile' | 'toll_free';
  display_name: string;
  usd_per_month: string;
}

export interface TelephonyFeeRow extends TelephonyProvenance {
  carrier: string;
  fee_kind: 'carrier_passthrough' | 'tcr_registration' | 'tcr_campaign_monthly';
  direction: 'outbound' | 'inbound' | 'na';
  usd_per_message?: string;
  usd_per_month?: string;
  usd_one_time?: string;
}

export interface TelephonyFile {
  version: 2;
  license: 'CC-BY-4.0';
  numbers: TelephonyNumberRow[];
  a2p_10dlc: TelephonyFeeRow[];
}

export type CostsFile<T> = {
  version: 2;
  license: 'CC-BY-4.0';
  models: T[];
};
