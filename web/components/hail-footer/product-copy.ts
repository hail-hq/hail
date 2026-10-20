/** Plain product copy shared by the website, directory listings, and footer snapshots. */
export const PRODUCT_COPY = {
  heading: "Give apps and AI agents a phone number and email inbox.",
  actions: "Make or take calls, send or answer texts and email.",
  assistant: "Let your AI assistant make and receive calls, send texts, and manage email.",
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
