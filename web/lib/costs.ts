import 'server-only';
import llmJson from '../../costs/llm.json';
import sttJson from '../../costs/stt.json';
import ttsJson from '../../costs/tts.json';
import twilioJson from '../../costs/twilio.json';
import telnyxJson from '../../costs/telnyx.json';
import didwwJson from '../../costs/didww.json';
import smsJson from '../../costs/sms.json';
import llmSchemaJson from '../../costs/schema/llm.schema.json';
import sttSchemaJson from '../../costs/schema/stt.schema.json';
import ttsSchemaJson from '../../costs/schema/tts.schema.json';
import numbersSchemaJson from '../../costs/schema/numbers.schema.json';
import smsSchemaJson from '../../costs/schema/sms.schema.json';
import type { CostsFile, LLMRow, STTRow, TTSRow, NumberCatalogFile, NumberRow, TelephonyFeeRow, SmsFile } from './types';

export const llm = llmJson as CostsFile<LLMRow>;
export const stt = sttJson as CostsFile<STTRow>;
export const tts = ttsJson as CostsFile<TTSRow>;
export const numberCatalogs = [twilioJson, telnyxJson, didwwJson] as NumberCatalogFile[];
/** Every carrier's rows in one list, each tagged with its carrier. */
export const numbers: NumberRow[] = numberCatalogs.flatMap((f) =>
  f.numbers.filter((n) => n.available !== false).map((n) => ({ ...n, provider: f.provider })),
);
/** Twilio's file also carries the US A2P 10DLC fee table. */
export const a2p10dlc = (numberCatalogs.find((f) => f.provider === 'twilio')?.a2p_10dlc ?? []) as TelephonyFeeRow[];
export const sms = smsJson as SmsFile;

export const llmSchema = llmSchemaJson;
export const sttSchema = sttSchemaJson;
export const ttsSchema = ttsSchemaJson;
export const numbersSchema = numbersSchemaJson;
export const smsSchema = smsSchemaJson;
