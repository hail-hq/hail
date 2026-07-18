import { llmSchema, smsSchema, sttSchema, ttsSchema, telephonySchema } from '@/lib/costs';

const SCHEMAS = {
  'llm.json': llmSchema,
  'stt.json': sttSchema,
  'tts.json': ttsSchema,
  'telephony.json': telephonySchema,
  'sms.json': smsSchema,
} as const satisfies Record<string, object>;

export const dynamic = 'force-static';

export function generateStaticParams() {
  return Object.keys(SCHEMAS).map((name) => ({ name }));
}

export async function GET(_req: Request, { params }: { params: Promise<{ name: string }> }) {
  const { name } = await params;
  if (!(name in SCHEMAS)) {
    return new Response('Not Found', { status: 404 });
  }
  const schema = SCHEMAS[name as keyof typeof SCHEMAS];
  return new Response(JSON.stringify(schema, null, 2), {
    headers: {
      'content-type': 'application/schema+json; charset=utf-8',
      'cache-control': 'public, max-age=0, s-maxage=3600, stale-while-revalidate=86400',
    },
  });
}
