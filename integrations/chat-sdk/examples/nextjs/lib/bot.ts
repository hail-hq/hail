import { Chat } from "chat";
import { createMemoryState } from "@chat-adapter/state-memory";
import { createRedisState } from "@chat-adapter/state-redis";
import { createHailAdapter } from "@hail-hq/chat-sdk-adapter";

if (process.env.NODE_ENV === "production" && !process.env.REDIS_URL) {
  throw new Error("Set REDIS_URL to persist Chat SDK state in production");
}

export const bot = new Chat({
  userName: "mybot",
  adapters: { hail: createHailAdapter() },
  state: process.env.REDIS_URL ? createRedisState() : createMemoryState(),
});

bot.onDirectMessage(async (thread, message) => {
  await thread.post(`You said: ${message.text}`);
});
