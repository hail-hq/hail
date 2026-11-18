/** Plain product copy shared by the website, directory listings, and footer snapshots. */
export const PRODUCT_COPY = {
  heading: "Give your AI a real phone number and email inbox.",
  actions: "Run a receptionist or answering service, and handle communication with MCP, API, and webhooks.",
  // The footer line. The home page closing heading says the same words, with links.
  tagline: "Phone numbers and email inboxes for AI. In minutes, not days.",
  assistant: "Let your AI assistant handle calls, texts, and email.",
  context: "Keep track of conversations across calls and texts.",
  numbers: "Get phone numbers in 120+ countries.",
  voice: "Choose a voice or bring your own.",
  availability: "Open-source or cloud priced on usage.",
} as const;

export const PRODUCT_SHORT_DESCRIPTION = `${PRODUCT_COPY.heading} ${PRODUCT_COPY.actions}`;

export const PRODUCT_LONG_DESCRIPTION = [
  PRODUCT_COPY.assistant,
  PRODUCT_COPY.context,
  PRODUCT_COPY.numbers,
  PRODUCT_COPY.voice,
  PRODUCT_COPY.availability,
].join(" ");
