# MCP file uploads: upload links and a verification check

## Problem

Two MCP tools take files only as base64 text inside the tool call:

- `upload_email_attachment(content_base64, ...)`
- `submit_verification(files=[{slot, content_type, content_base64}])`

The model has to write every character of the file into the call. A 100 KB
PDF is about 136,000 characters. In practice the call is slow, hits the
client's output limit, or never gets sent at all. One wrong character corrupts
the file, and a verification cannot be undone once filed.

The MCP server authenticates with the user's OAuth token. The agent's shell
(where `curl` runs) has no Hail credentials. So a fix must give the agent a
URL that carries its own one-time permission.

## Decisions

- One shared upload link flow for email attachments and verification
  documents.
- The link points at the Hail API, not at S3. The API streams the body,
  enforces the size limit, checks the file type, and returns `size_bytes` and
  `sha256` in the upload response. (An S3 presigned PUT cannot cap size or
  return a checksum.)
- A separate read-only check for verifications that runs every check Hail can
  run without filing anything at the carrier.
- The base64 paths stay, for very small files. Their descriptions point to the
  upload link for anything larger.
- Not in scope: fetching a file from a URL, reusing inbound email
  attachments, claude.ai web chat (it has no shell to run `curl`).

## API

### `POST /v1/uploads` (authenticated)

Body (JSON):

| field          | type   | notes                                                                      |
| -------------- | ------ | -------------------------------------------------------------------------- |
| `purpose`      | string | `email_attachment` or `verification_document`                              |
| `filename`     | string | 1 to 255 chars                                                             |
| `content_type` | string | `verification_document`: `image/jpeg`, `image/png`, `application/pdf` only |

Creates a pending `file_uploads` row (see Storage) and returns 201:

```json
{
  "id": "uuid",
  "purpose": "verification_document",
  "upload_url": "https://api.hail.so/v1/uploads/<id>?token=<secret>",
  "method": "PUT",
  "content_type": "image/jpeg",
  "max_bytes": 10485760,
  "expires_at": "2026-11-09T18:15:00Z"
}
```

- The token is 32 random bytes, URL-safe. Only its sha256 is stored.
- The link expires 15 minutes after it is made and works once.
- `upload_url` is built from `settings.hail_api_url` (the public API URL; in
  the cloud this is `https://api.hail.so`, same as unsubscribe links).
- An organization may hold at most 50 pending, unexpired links. The 51st is a 429.
- 503 when the mail bucket is not configured (same as
  `POST /v1/email-attachments`).

### `PUT /v1/uploads/{id}?token=...` (no Authorization header)

- The token is the only credential. Compare its hash in constant time.
- Unknown id, wrong token, or a link already used: 404 (one message, so the
  response does not reveal which). Expired: 410.
- The body is the raw file bytes. Read in 1 MB chunks. Stop and return 413
  (or 422 to match the existing upload route; pick one and document it) once
  the purpose's limit is passed: email uses `MAX_EMAIL_ATTACHMENT_BYTES`,
  verification uses the verification `MAX_FILE_BYTES` (10 MB).
- Empty body: 422.
- `verification_document`: the first bytes must match the declared type
  (JPEG, PNG or PDF magic bytes). Move the MCP's `_matches_declared_type`
  logic to `core` and use it in both places.
- The declared `content_type` from the link is what is stored. The request's
  `Content-Type` header is not trusted.
- `email_attachment`: store the object where direct email attachment
  uploads go. The same row is now an uploaded email
  attachment, so its id works in `attachment_ids`, and the existing 24 hour
  unused-upload GC covers it.
- `verification_document`: store the object in its own per-organization
  folder for verification documents.
- Mark the link used (clear `token_hash`, set `uploaded_at`, `size_bytes`,
  `sha256`) in the same transaction, guarded so two concurrent PUTs cannot
  both succeed.
  - The link has no in-between state. Each PUT writes its own object key
    (the link's key plus `.<attempt>`), then one atomic update marks the link
    used and points it at that key.
  - The PUT that loses a race deletes its own object. The winner deletes any
    other attempt objects. Cleanups delete the upload's whole key prefix.
  - The body must arrive before the expiry (410 if the link is already
    expired when the PUT starts). Storing it may end up to 15 minutes after
    the expiry. Any PUT whose final update matches nothing (another PUT won,
    the link was removed, or storing ended later than that) deletes its own
    object and answers 404 "make a new link if your file is not stored"; a
    second upload of the same file is harmless. The link sweep takes only
    links more than 15 minutes past the expiry, so the PUT and the sweep
    never take the same row. Every upload time is on the database clock. The sweep
    deletes the row (only while still unused), then the objects. A failed S3
    delete after that leaves an object: verification documents have the
    1-day lifecycle rule as a backstop, and email attachment leftovers are
    accepted (rare, logged).
  - Cleanups list the upload's prefix, so the API needs `s3:ListBucket` on
    the bucket ARN with an `s3:prefix` condition for the upload folders
    (`infra/terraform/iam.tf`).
  - Body reads are capped per process (8) and per organization (2). Over
    either cap: 503 with `Retry-After`, and the link still works.
- Response 200: `{id, purpose, filename, content_type, size_bytes, sha256}`.
- Must be reachable without auth. Check the rate limiter and any auth
  middleware let a request with no bearer through. The general limit keys
  the PUT on the caller's IP. In prod the API trusts the proxy's
  `X-Forwarded-For` (`FORWARDED_ALLOW_IPS` in `docker-compose.prod.yml`, safe
  because the API port is bound to localhost), so each caller has its own
  bucket instead of all sharing the proxy's IP.

### Telemetry

The token must never reach Logfire. The FastAPI instrumentation records
`url.full` / `http.url` with the query string. Redact the `token` query value
in the scrubbing callback in `core/hailhq/core/telemetry.py` (or strip it in a
request hook) and add a test. Do not log the token anywhere.

### `create_verification` changes

- New form part `upload.<slot>` = an upload id, as an alternative to the file
  part `file.<slot>`. Both for one slot: 422.
- The upload must belong to the caller's organization, have purpose
  `verification_document`, be uploaded, and be under 1 hour old. Otherwise
  422 naming the slot (never echo other data).
- Bytes are read from S3 server side. The MCP never downloads them.
- After the verification row is committed, delete the used uploads (S3 object
  and row). Best effort: a failure is logged as a warning, and GC removes
  them later. If the submit fails (422 from the carrier, 402, 409), the
  uploads stay so the agent can fix the form and try again.

### `POST /v1/verifications/check` (authenticated, new)

Same form as `POST /v1/verifications` (`dry_run` is not a part; it is its own
route so it is read-only in OpenAPI and in MCP annotations).

Runs the same steps as submit up to, and not including,
`provider.create_draft`:

1. form parsing and the purpose rules
2. quote / number lookups, provider pick
3. the credit check (402)
4. the carrier requirements read (a read, nothing filed)
5. the duplicate check (409)
6. `length_problems`
7. file reads (`file.<slot>` and `upload.<slot>`), type and size checks
8. SMS: `_sms_mobile_problems`, the filing-fee credit check
9. Local form check: `validate_input(...)` for `purpose=number`,
   `provider.validate_application(...)` for `purpose=sms`

No `org_lock`, no hold, no row, no carrier write, no charge. Uploads are not
consumed.

Errors are the same codes submit gives. A pass returns 200:

```json
{
  "ok": true,
  "files": [
    {
      "slot": "id_front",
      "upload_id": "uuid",
      "size_bytes": 1,
      "sha256": "..."
    }
  ],
  "note": "The carrier runs its own checks when you submit."
}
```

Local problems from step 9 return 422 in the same shape submit uses.

Implement by extracting the shared steps from `create_verification` into one
helper both routes call. Submit's behavior must not change: the existing
verification tests must pass unchanged.

## Storage

The repo's schema rules (`CLAUDE.md`, "Schema changes") say to extend the
closest table. `email_attachment_uploads` already holds org-owned uploaded
files. Only hail reads it (`routes/email_attachments.py`, `routes/emails.py`,
`email_attachment_gc.py`); hail-website does not.

The migration:

- Rename `email_attachment_uploads` to `file_uploads` (and its indexes).
  Rename the model `EmailAttachmentUpload` to `FileUpload`.
- Add `purpose text not null default 'email_attachment'`, CHECK in
  (`email_attachment`, `verification_document`).
- Add `token_hash text null` (sha256 hex of the link token; null once used or
  for direct uploads), `link_expires_at timestamptz null`,
  `uploaded_at timestamptz null`, `sha256 text null`.
- Make `size_bytes` nullable. CHECK: `uploaded_at is null or size_bytes is
not null`.
- Backfill existing rows: `uploaded_at = created_at`.
- `POST /v1/email-attachments` (direct multipart) also sets `uploaded_at` and
  `sha256`.
- `POST /v1/emails` resolves `attachment_ids` only from rows with
  `purpose = 'email_attachment'` and `uploaded_at is not null`. A pending link
  id or a verification document id is "not found", same as today.
- Partial index for the link sweep: `(link_expires_at) where uploaded_at is
null`.

The migration docstring names the table checked and why it was extended, per
the schema rules. Downgrade reverses it (delete `verification_document` and
pending rows first, then drop the columns and rename back).

Check organization-deletion / retention code (`core/hailhq/core/retention.py`,
org closures) and add the verification documents' folder wherever per-org
stored files are purged.

## GC

Extend the existing hourly attachment GC worker (or add a sibling in the same
lifespan block) to delete:

- rows never uploaded, past `link_expires_at` (no S3 object exists)
- `verification_document` rows uploaded more than 1 hour ago (S3 object, then
  row)
- the existing rule stays for `email_attachment` rows: unused 24 hours after
  upload

Run the verification sweep often enough that a passport scan does not stay
much past 1 hour (for example every 10 minutes).

## MCP

- New tool `create_file_upload(purpose, filename, content_type)`.
  Annotations: not read-only, not destructive. Returns the API response plus
  a ready `curl` line:
  `curl -sS -X PUT --data-binary @<path to the file> -H 'Content-Type: <type>' '<upload_url>'`
  and one sentence: run it from a shell, the response has `size_bytes` and
  `sha256`; compare them with the local file.
- `upload_email_attachment`: description says base64 is for small files
  (under about 50 KB). Larger files: `create_file_upload` with purpose
  `email_attachment`, then pass the returned id in `send_email`'s
  `attachment_ids`. State the size limit.
- `submit_verification`: each `files` entry is either
  `{slot, upload_id}` or `{slot, content_type, content_base64}`. Validate
  `upload_id` as a UUID; send it as the `upload.<slot>` part. Keep every
  current error message rule (never echo a value). Describe max size (10 MB
  each, the total limit) and types (JPG, PNG, PDF).
- New tool `check_verification`, same arguments as `submit_verification`
  except `attest_authorized`. Read-only, not destructive. Calls
  `POST /verifications/check`.
- `HailClient`: `create_file_upload`, `check_verification`, and
  `submit_verification` accepting upload ids.

## Other surfaces

- OpenAPI spec regenerated the way the repo does it.
- Python SDK and the Go CLI generated client regenerated if they are built
  from the spec (see how the 2026-07-06 email attachments plan did it).
- `docs/public/mcp.md` tool table, `docs/public/email.md` (upload link
  example), the verification docs page if there is one, and
  `docs/submissions/claude-ai-connectors-directory.md` tool list.
- `CHANGELOG.md`: one short user-facing line.
- Nothing about any customer in code, docs, commits or the PR.

## Tests

- Upload link: create, PUT ok (size + sha256 correct), second PUT 404,
  wrong token 404, expired 410, too large rejected, wrong magic bytes for a
  verification document rejected, 51st pending link 429, email upload id
  works in `POST /v1/emails` `attachment_ids`.
- Token redaction in telemetry.
- `create_verification` with `upload.<slot>`: works, wrong org / wrong
  purpose / too old rejected, both parts for one slot rejected, uploads
  deleted after success, kept after a failed submit.
- `/verifications/check`: pass returns files with sha256; a missing field
  returns 422; nothing filed (provider `create_draft` never called); no row
  written; no hold.
- MCP: new tools, `files` entries with `upload_id`, error messages never echo
  values.
- GC sweeps.
