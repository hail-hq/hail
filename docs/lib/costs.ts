import 'server-only';
import llmJson from '../../costs/llm.json';
import sttJson from '../../costs/stt.json';
import ttsJson from '../../costs/tts.json';
import type { CostsFile, LLMRow, STTRow, TTSRow } from './types';

export const llm = llmJson as CostsFile<LLMRow>;
export const stt = sttJson as CostsFile<STTRow>;
export const tts = ttsJson as CostsFile<TTSRow>;
