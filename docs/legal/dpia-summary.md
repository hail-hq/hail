# Data Protection Impact Assessment — Summary

Hail conducts a Data Protection Impact Assessment (GDPR Art. 35) for its
outbound AI voice-calling activity, and treats it as the pre-launch gate
for any new communication channel (including SMS/text messaging) before
that channel goes live for real users. This page summarizes the
assessment; it is prepared internally, with legal sufficiency review
ongoing.

## Why a DPIA

Outbound AI voice calling combines new/emerging technology (a real-time
speech-to-text → LLM → text-to-speech pipeline), a chain of third-party
sub-processors handling conversation content, and the realistic
possibility that a call could incidentally touch sensitive topics.
Recipients — the people a Developer calls through Hail — are third
parties who never signed up for the service and have no direct
relationship with Hail. Each of these is independently sufficient to
warrant an assessment under the ICO/EDPB screening criteria, so we treat
this as required rather than optional.

## What we assessed

- **Nature of the processing** — how a call flows through the platform:
  Developer trigger → carrier dial-out → speech-to-text → LLM reasoning
  → text-to-speech → stored transcript. See our
  [Privacy Policy](https://hail.so/legal/privacy) for the full data-flow
  description and [sub-processor list](https://hail.so/legal/subprocessors).
- **Data minimization** — call audio is not recorded or retained; only a
  text transcript is stored. Voice output is limited to a fixed set of
  platform-provided voices — Developers cannot supply or select a custom
  or cloned voice, which closes off voice-cloning misuse as a platform
  capability rather than relying solely on policy to prohibit it.
- **Necessity and proportionality** — each sub-processor in the pipeline
  serves a distinct, non-redundant role; the LLM providers are chained as
  failover for reliability, not parallel processing, so a given
  conversation is only ever sent to one provider at a time.
- **Consent and lawful basis** — Developers (Hail's customers) warrant
  that they hold the lawful basis and consent required to contact each
  Recipient, per our [Acceptable Use Policy](https://hail.so/legal/aup)
  and [Terms of Use](https://hail.so/legal/terms). We continue to invest
  in account-level compensating controls (signup verification, rate
  limiting) as part of our overall risk posture.
- **International transfers** — Hail operates as a global service.
  Where a transfer requires a safeguard under EU/UK data protection law,
  Standard Contractual Clauses are incorporated by reference in our
  [DPA](https://hail.so/legal/dpa), applied as a blanket safeguard.
- **Security measures** — encryption in transit and at rest, access
  controls, least-privilege, and audit logging, detailed in our DPA's
  Annex II.

## Outcome

Based on this assessment, we consider the residual risk of the outbound
voice-calling activity acceptable to proceed at Hail's current scale.
This is reviewed and re-assessed before general availability, before any
new communication channel launches, whenever a new sub-processor is
added, on any material change to the voice pipeline, and periodically as
usage grows.

_This summary is not a substitute for the full internal risk assessment
and does not constitute legal advice._
