import 'server-only';
import llmJson from '../../costs/llm.json';
import sttJson from '../../costs/stt.json';
import ttsJson from '../../costs/tts.json';
import telephonyJson from '../../costs/telephony.json';
import smsJson from '../../costs/sms.json';
import llmSchemaJson from '../../costs/schema/llm.schema.json';
import sttSchemaJson from '../../costs/schema/stt.schema.json';
import ttsSchemaJson from '../../costs/schema/tts.schema.json';
import telephonySchemaJson from '../../costs/schema/telephony.schema.json';
import smsSchemaJson from '../../costs/schema/sms.schema.json';
import type { CostsFile, LLMRow, STTRow, TTSRow, TelephonyFile, SmsFile } from './types';

export const llm = llmJson as CostsFile<LLMRow>;
export const stt = sttJson as CostsFile<STTRow>;
export const tts = ttsJson as CostsFile<TTSRow>;
export const telephony = telephonyJson as TelephonyFile;
export const sms = smsJson as SmsFile;

export const llmSchema = llmSchemaJson;
export const sttSchema = sttSchemaJson;
export const ttsSchema = ttsSchemaJson;
export const telephonySchema = telephonySchemaJson;
export const smsSchema = smsSchemaJson;
