# hail-sdk

Python SDK for [Hail](https://hail.so) — universal communication platform for AI agents.

## Install

```bash
pip install hail-sdk
```

Requires Python 3.11+.

## Quickstart

```python
import asyncio
from hail import Client

async def main():
    async with Client(api_key="sk-...") as client:
        # Place an outbound call.
        call = await client.calls.create(
            to="+15551234567",
            system_prompt="You are calling to confirm a reschedule.",
        )
        print("queued", call.id)

        async for event in client.events.tail(id=f"call:{call.id}"):
            print(event.kind, event.payload)

        # Send an outbound email.
        email = await client.emails.create(
            to=["alice@example.com"],
            subject="Welcome",
            body_text="Thanks for signing up.",
        )
        print(email.status, email.from_address)

asyncio.run(main())
```

`api_key` defaults to `$HAIL_API_KEY`; `base_url` defaults to `$HAIL_API_URL`
(falling back to `https://api.hail.so`).

## API surface

### Calls

- `client.calls.create(*, to, system_prompt=None, llm=None, from_=None, first_message=None, metadata=None, idempotency_key=None)` — originate an outbound call. Pass either `system_prompt` (mode A) or a full `llm` block (mode B). `idempotency_key` defaults to a fresh UUIDv4.
- `client.calls.get(call_id)` — fetch a single call.
- `client.calls.list(*, cursor=None, limit=50, status=None, to=None)` — cursor-paginated org list.

### Emails

- `client.emails.create(*, to, subject, body_text=None, body_html=None, from_=None, cc=None, bcc=None, reply_to=None, conversation_id=None, metadata=None, idempotency_key=None)` — send an outbound email. At least one of `body_text` / `body_html` is required. `from_` is optional while the org has one verified sender; with several the server returns 422 and you must name one (read `default_from` from `client.email_domains.list()`). With none it auto-mints a hail-mail address (operator-configured). `idempotency_key` defaults to a fresh UUIDv4.
- `client.emails.get(email_id)` — fetch a single email.
- `client.emails.list(*, cursor=None, limit=50, status=None)` — cursor-paginated org list.

### Identity

- `client.whoami()` — who the API key belongs to: `auth_kind`, `organization_id`, `user_id`, `email`, `name`. The user fields are `None` for a shared operator key. Use `email` as `reply_to` so replies reach the person, not the sending domain.

### Events

- `client.events.list(*, id=None, kind=None, cursor=None, limit=100)` — one-shot fetch of `GET /events`. `id` is a typed `<type>:<uuid>` string (v1 only supports `call:`).
- `client.events.tail(*, id=None, kind=None, interval_seconds=0.5, follow=True)` — async-iterator tail; auto-exits on terminal call status when narrowed to `id=call:<uuid>`.

## Errors

All SDK errors derive from `hail.HailError`. HTTP-status-coded errors carry `.status_code`, `.detail`, and `.response_text`:

- `HailAuthError` (401)
- `HailNotFoundError` (404)
- `HailIdempotencyConflict` (409)
- `HailValidationError` (422)
- `HailServerError` (5xx, after retry budget)
- `HailClientError` (other 4xx)
- `HailMalformedResourceId` — local validation of `<type>:<uuid>` strings
- `HailConfigError` — no API key supplied or discoverable

## Retries

GET/HEAD/PUT/DELETE — and any POST/PATCH carrying an `Idempotency-Key` — are retried up to 3 times on 5xx with exponential backoff (0.5s, 1.0s, 2.0s) plus jitter, honoring `Retry-After` when present. Mutating verbs without an idempotency key fail fast on 5xx so a duplicate side effect can't be silently introduced.

## License

[AGPL-3.0-or-later](https://github.com/hail-hq/hail/blob/main/LICENSE).
