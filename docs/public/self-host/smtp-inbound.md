# SMTP inbound — not yet implemented

The `SmtpInboundProvider` interface exists in
[`core/hailhq/core/providers/email/inbound/smtp.py`](https://github.com/hail-hq/hail/blob/main/core/hailhq/core/providers/email/inbound/smtp.py)
but is not implemented. It is the cloud-agnostic / OSS-only path.
We defer it to a follow-up milestone.

When it is released, this page will describe:

- the `mailbot/` container (`aiosmtpd`-backed), parallel to `voicebot/`
- listen ports + TLS configuration
- the "front me with Maddy or Postfix" production recipe for SPF / DKIM /
  DMARC verification and flood resistance
- self-host quickstart

Until then, use the SES-backed inbound path documented in
[AWS SES](./aws-ses.md). If you must avoid AWS, file an
issue that tracks your need. Then we can give the SMTP listener priority.

## References

- Design spec: [`docs/superpowers/specs/2026-06-06-inbound-email-design.md`](https://github.com/hail-hq/hail/blob/main/docs/superpowers/specs/2026-06-06-inbound-email-design.md) §2
- Provider interface: [`core/hailhq/core/providers/email/inbound/base.py`](https://github.com/hail-hq/hail/blob/main/core/hailhq/core/providers/email/inbound/base.py)
- Placeholder stub: [`core/hailhq/core/providers/email/inbound/smtp.py`](https://github.com/hail-hq/hail/blob/main/core/hailhq/core/providers/email/inbound/smtp.py)
