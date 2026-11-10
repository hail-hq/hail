/** Resolved for every request so rotated and short-lived credentials take effect. */
export type HailCredential = string | (() => string | Promise<string>);

export interface HailAdapterConfig {
  apiKey?: HailCredential;
  webhookSecret?: HailCredential;
  /** Hail-owned SMS-capable number, in E.164 format. */
  fromNumber?: string;
  /** API origin, without /v1. Defaults to HAIL_API_URL or https://api.hail.so. */
  apiUrl?: string;
  userName?: string;
  /** Explicit attestation, or a per-recipient check. No sends are allowed by default. */
  recipientConsent?: boolean | ((to: string) => boolean | Promise<boolean>);
  fetch?: typeof globalThis.fetch;
}

export interface HailThreadId {
  fromNumber: string;
  peerNumber: string;
}

/** Fields consumed from Hail's public SmsResponse; additional API fields remain on raw. */
export interface HailSms {
  id: string;
  from_e164: string;
  to_e164: string;
  direction: "inbound" | "outbound";
  body: string;
  requested_at: string;
  status?: string;
}
