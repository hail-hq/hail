# SMTP inbound — not yet implemented

The `SmtpInboundProvider` interface exists in
[`core/hailhq/core/providers/email/inbound/smtp.py`](../../core/hailhq/core/providers/email/inbound/smtp.py)
but is not implemented. It is the cloud-agnostic / OSS-only path,
deferred to a follow-up milestone.

When it lands, this page will describe:

- the `mailbot/` container (`aiosmtpd`-backed), parallel to `voicebot/`
- listen ports + TLS configuration
- the "front me with Maddy or Postfix" production recipe for SPF / DKIM /
  DMARC verification and flood resistance
- self-host quickstart

In the meantime, use the SES-backed inbound path documented in
[`docs/setup/aws-ses.md`](aws-ses.md). If you must avoid AWS, file an
issue tracking your need so the SMTP listener gets prioritized.

## References

- Design spec: [`docs/superpowers/specs/2026-06-06-inbound-email-design.md`](../superpowers/specs/2026-06-06-inbound-email-design.md) §2
- Provider interface: [`core/hailhq/core/providers/email/inbound/base.py`](../../core/hailhq/core/providers/email/inbound/base.py)
- Placeholder stub: [`core/hailhq/core/providers/email/inbound/smtp.py`](../../core/hailhq/core/providers/email/inbound/smtp.py)
