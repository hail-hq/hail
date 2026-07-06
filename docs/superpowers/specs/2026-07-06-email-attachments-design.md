# Outbound email attachments

Status: approved
Repo: `hail/` (core/api/mcp/cli/sdk/openapi/docs/infra)

## Goal

`POST /emails` and the MCP `send_email` tool currently have no way to attach a
file — `EmailCreate` uses `extra="forbid"` and no caller (API, MCP, SDK)
exposes an attachment parameter. The provider layer (`EmailProvider.send_email`,
SES's raw-MIME path) already supports attachments; nothing calls it that way
for user-composed sends. This spec adds outbound attachments end to end:
upload once, reference by id, send.

Inbound attachments (reading files off received mail) already work and are
out of scope here except where their storage (`S3InboundClient`, the shared
bucket) is reused/renamed for outbound.

## Decisions (locked)

1. **Upload-then-reference, not inline bytes.** Callers `POST
/email-attachments` first to get an `attachment_id`, then pass
   `attachment_ids` into `POST /emails`. Rejected alternatives: base64 bytes
   inline on `EmailCreate` (payload bloat, awkward for large files) and
   caller-hosted URL fetch (SSRF risk, external dependency in the send path).
2. **Upload endpoint is `multipart/form-data`**, not base64 JSON — standard
   for binary payloads, no encoding bloat, streams to S3. This is a new
   pattern for this API (no prior file-upload endpoint existed); everywhere
   else stays JSON.
3. **Reusable, org-scoped attachment ids.** One uploaded file can back
   multiple separate `send_email` calls (e.g. one PDF invoice attached to 50
   emails) — not consumed on first use. Simpler than single-use bookkeeping
   and matches the natural "upload once, send many" pattern.
4. **24h expiry for never-used uploads.** A periodic job deletes the S3
   object + row for any upload where `first_used_at IS NULL` and it's older
   than 24h, bounding storage cost from abandoned uploads. Used attachments
   are kept indefinitely (reusable model).
5. **10MB total cap per email** (body + all attachments combined), matching
   SES's `SendRawEmail` default hard limit — enforced by Hail before the send
   attempt so callers get a clear 422 instead of an opaque provider error.
   Exceeding it returns: _"attachment(s) too large — host the file externally
   and include a link in the body instead."_ Same message text used by the
   API, the MCP tool's error surface, and the CLI.
6. **No MIME allowlist/denylist in v1.** Accept any content type; rely on the
   size cap plus the existing consent/suppression compliance gate. Add
   restrictions later only if abuse is observed (YAGNI).
7. **No new S3 bucket/env var for outbound** — reuse the existing
   inbound-mail bucket under a new key prefix. Rename it to a generic name
   since it's fine to drop/recreate the bucket (no data migration needed):
   see "Storage rename" below.
8. **CLI gets a convenience `--attach` flag** on `hail email send` (repeatable,
   takes a local file path, uploads then sends in one command) in addition to
   whatever raw commands the OpenAPI codegen produces for
   `POST /email-attachments`.

## Data model

New table `EmailAttachmentUpload` (`core/hailhq/core/models.py`), separate
from the existing `EmailAttachment` table because the lifecycles genuinely
differ: `EmailAttachment` rows are always 1:1 with an already-received
inbound email, created once at ingest, never reused. `EmailAttachmentUpload`
rows are pre-send, org-owned (not yet tied to any `Email`), reusable across
sends, and expire if unused.

- `id: UUID` — PK
- `organization_id: UUID` — owner, checked on every reference
- `filename: Text`, `content_type: Text`, `size_bytes: Integer`
- `s3_key: Text` — `outbound-attachments/{organization_id}/{id}`
- `created_at: TIMESTAMP`
- `first_used_at: TIMESTAMP | None` — set the first time a send references
  this id; GC only considers rows where this is still null

On send, one `EmailAttachment` row is created per referenced upload —
pointing at the _same_ `s3_key` (no S3 copy) — so `GET /emails/{id}` and the
existing `GET /emails/{id}/attachments/{attachment_id}` presign/redirect work
identically for outbound and inbound rows, with no new read-side code.

## API

**`POST /email-attachments`** (multipart/form-data, org-scoped by API key)

- Validates size ≤ 10MB, uploads to S3 via `S3MailClient.put_attachment` at
  `outbound-attachments/{org_id}/{attachment_id}`, inserts the row.
- Returns `{id, filename, content_type, size_bytes}`.
- Over-size upload → 422 with the standard oversize message (decision 5).

**`EmailCreate.attachment_ids: list[UUID] | None`** (new field)

- On `POST /emails`, before the `Email` row is created: resolve each id
  scoped to the caller's org (404 if missing or belongs to another org), sum
  `size_bytes` against the message body, reject over the 10MB cap (decision
  5). This runs alongside the existing pre-row checks (consent, compliance
  gate, balance).
- On success: fetch each payload from S3, build `ProviderAttachment` list,
  pass into `email_provider.send_email(...)` (already accepts `attachments`;
  the SES raw-MIME path exists today and simply never receives any). After
  the `Email` row commits, insert the `EmailAttachment` rows (see Data
  model) and stamp `first_used_at` on any upload row where it's still null.

## Storage rename

No functional change to inbound behavior — pure rename so the bucket/client
name reflects that it now holds both inbound raw MIME and outbound
attachments. Acceptable to drop and recreate the bucket (no migration).

- `core/hailhq/core/s3_inbound.py` → `core/hailhq/core/s3_mail.py`;
  `S3InboundClient` → `S3MailClient` (methods unchanged: `fetch_raw`,
  `put_attachment`, `presign_get`).
- `config.py`: `hail_inbound_email_name_prefix` → `hail_mail_name_prefix`;
  computed `hail_inbound_bucket` → `hail_mail_bucket`, derived as
  `{prefix}-mail` (was `-raw`).
- `.env.example`: `HAIL_INBOUND_EMAIL_NAME_PREFIX` → `HAIL_MAIL_NAME_PREFIX`;
  comment updated to describe the bucket as backing both inbound and
  outbound mail storage.
- Update call sites: `api/hailhq/api/main.py`, `api/hailhq/api/routes/emails.py`,
  `api/hailhq/api/routes/internal/ses_events.py`,
  `core/hailhq/core/outbound_worker.py`, `core/hailhq/core/email_ingest.py`,
  and their tests (`core/tests/test_s3_inbound.py` →
  `core/tests/test_s3_mail.py`, plus the internal-ses-events and
  emails-inbound-reads test fixtures that reference the client/bucket).
- Fix a pre-existing docs bug found during exploration: `docs/setup/aws-ses.md`
  and `docs/operations.md` currently document a `HAIL_INBOUND_BUCKET` env var
  that doesn't exist in code (the bucket has only ever been a computed
  field). Corrected to `HAIL_MAIL_NAME_PREFIX` while these docs are touched.
- `infra/terraform/`: `s3_inbound.tf` → `s3_mail.tf`, bucket name
  `${name_prefix}-mail`, `outputs.tf`/`main.tf` updated to match.

## MCP / SDK / CLI

- New MCP tool `upload_email_attachment(content_base64, filename,
content_type)`. MCP tool args are JSON-only, so the agent-facing surface
  stays base64-in-JSON; the MCP server decodes and does the multipart POST
  to the API internally. Returns the same `{id, filename, content_type,
size_bytes}` shape.
- `send_email` MCP tool gains `attachment_ids: list[str] | None`.
- Python SDK: matching `upload_email_attachment`/`send_email(attachment_ids=...)`
  wrappers, mirroring the existing `_format_api_error`/`ValidationError`
  handling pattern already used by every other SDK function in
  `mcp/hailhq/mcp/tools.py`.
- Go CLI: generated client methods for `POST /email-attachments` come free
  from the OpenAPI codegen. Hand-written addition: repeatable `--attach
<path>` flag on `hail email send` that reads the local file, uploads it,
  and folds the resulting id(s) into the send call.

## Oversize error handling

Every layer that can reject for size — the upload endpoint (single file >
10MB) and the send endpoint (aggregate > 10MB) — returns the same 422 detail
text (decision 5), and the MCP tool / CLI surface that detail verbatim rather
than a generic "request failed." This was called out explicitly as a UX
requirement: a caller (human or agent) hitting the cap should be told to link
the file instead of getting a bare validation error.

## Documentation & release

- OpenAPI: `POST /email-attachments` path, updated `EmailCreate`/`EmailResponse`
  schemas — regenerated in the same PR per the existing "OpenAPI is source of
  truth for the CLI" invariant.
- `docs/setup/aws-ses.md`, `docs/operations.md`: bucket rename (see above).
- Wherever email sending is documented for MCP/CLI/API usage: add an
  attachment example (upload → reference → send).
- `CHANGELOG.md`: new entry under "Unreleased", grouped by area (API, MCP,
  CLI), following the existing convention (see e.g. the email-deliverability
  and cartesia-fallback CHANGELOG entries for format).

## Testing

- Unit: size-cap rejection (single upload, aggregate at send), org-scoping
  (cross-org id reference → 404), reusability (same id used by two sends),
  GC job (unused row past 24h deleted; used row untouched regardless of age).
- Provider layer: SES raw-MIME path already has attachment coverage per
  existing `_build_raw_mime` tests — extend if the new call path exposes a
  gap (e.g. multiple attachments, mixed content types).
- Rename: existing `S3InboundClient`/`hail_inbound_bucket` test coverage
  moves to the new names with no behavior change; add a regression test
  confirming `docs/setup/aws-ses.md`'s env var name matches `.env.example`
  (closes the discrepancy found during exploration).
- Integration: end-to-end upload → send → `GET /emails/{id}` shows the
  attachment → `GET /emails/{id}/attachments/{id}` redirects to a working
  presigned URL, exercised for both a fresh upload and a reused one.
