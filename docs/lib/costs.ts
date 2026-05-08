import 'server-only';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import type { CostsFile, LLMRow, STTRow, TTSRow } from './types';

const REPO_ROOT = join(process.cwd(), '..');

async function loadFile<T>(name: 'llm' | 'stt' | 'tts'): Promise<CostsFile<T>> {
  const path = join(REPO_ROOT, 'costs', `${name}.json`);
  const raw = await readFile(path, 'utf-8');
  return JSON.parse(raw) as CostsFile<T>;
}

export async function loadLLM(): Promise<CostsFile<LLMRow>> {
  return loadFile<LLMRow>('llm');
}
export async function loadSTT(): Promise<CostsFile<STTRow>> {
  return loadFile<STTRow>('stt');
}
export async function loadTTS(): Promise<CostsFile<TTSRow>> {
  return loadFile<TTSRow>('tts');
}
