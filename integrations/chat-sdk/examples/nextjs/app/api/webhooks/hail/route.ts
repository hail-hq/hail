import { after } from "next/server";
import { bot } from "../../../../lib/bot";

export const runtime = "nodejs";
export async function POST(request: Request): Promise<Response> {
  return bot.webhooks.hail(request, { waitUntil: (task) => after(() => task) });
}
