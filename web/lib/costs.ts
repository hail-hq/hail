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
import type { CapabilityMonthlyFee, CostsFile, LLMRow, STTRow, TTSRow, NumberCatalogFile, NumberRow, TelephonyFeeRow, SmsFile, TelephonyNumberRow } from './types';

export const llm = llmJson as CostsFile<LLMRow>;
export const stt = sttJson as CostsFile<STTRow>;
export const tts = ttsJson as CostsFile<TTSRow>;
export const numberCatalogs = [twilioJson, telnyxJson, didwwJson] as NumberCatalogFile[];

const cents = (usd: string) => Math.round(Number(usd) * 100);

/** The real monthly price of a number with everything it can do switched on.
 * Same rule as monthly_price in core/hailhq/core/telephony_catalog.py: each fee
 * counts once. A buyer who leaves the capability off pays `usd_per_month`. */
function withMonthlyFees(n: TelephonyNumberRow, fees: CapabilityMonthlyFee[] = []) {
  const has = (['voice', 'sms', 'mms'] as const).filter((c) => n[c]);
  const monthly_fees = fees.flatMap((fee) => {
    const matched = has.filter((c) => fee.capabilities.includes(c));
    if (matched.length === 0) return [];
    return [{ label: matched.join('/').toUpperCase(), usd_per_month: fee.usd_per_month }];
  });
  if (monthly_fees.length === 0) return { monthly_fees, total_usd_per_month: n.usd_per_month };
  const total = monthly_fees.reduce((sum, fee) => sum + cents(fee.usd_per_month), cents(n.usd_per_month));
  return { monthly_fees, total_usd_per_month: (total / 100).toFixed(2) };
}

/** Every carrier's rows in one list, each tagged with its carrier. */
export const numbers: NumberRow[] = numberCatalogs.flatMap((f) =>
  f.numbers
    .filter((n) => n.available !== false)
    .map((n) => ({ ...n, provider: f.provider, ...withMonthlyFees(n, f.capability_monthly_fees) })),
);
/** A number a customer can buy in the console or the API. The others are
 * listed, labelled, and kept out of "from" prices. */
export function onSale(n: NumberRow): boolean {
  return !n.receive_only && !n.by_request;
}
/** Twilio's file also carries the US A2P 10DLC fee table. */
export const a2p10dlc = (numberCatalogs.find((f) => f.provider === 'twilio')?.a2p_10dlc ?? []) as TelephonyFeeRow[];
export const sms = smsJson as SmsFile;

export const llmSchema = llmSchemaJson;
export const sttSchema = sttSchemaJson;
export const ttsSchema = ttsSchemaJson;
export const numbersSchema = numbersSchemaJson;
export const smsSchema = smsSchemaJson;
