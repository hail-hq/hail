# MCP email read-parity — design

**Date:** 2026-06-13
**Status:** approved, pending implementation

## Problem

The MCP server exposes read tools for calls (`get_call`, `list_calls`) but
none for email. An agent can `send_email` and then has no way to read replies
or any received mail through the MCP — even though the REST API already serves
them via `GET /emails` (with a `direction=inbound` filter) and
`GET /emails/{id}`. This blind spot is concrete: an agent that sends a
confirmation cannot check whether the recipient replied.

## Goal

Give the agent the same email read surface it already has for calls. Mirror
`get_call` / `list_calls` exactly. Nothing more.

## Scope

Two new MCP tools:

```
get_email(email_id) -> EmailResponse dict | {"error": ...}
list_emails(cursor=None, limit=50, status=None, direction=None)
    -> {"items": [...], "next_cursor": <str|None>} | {"error": ...}
```

- `status` ∈ `queued|sent|failed|bounced|complained|received`
- `direction` ∈ `outbound|inbound` — `direction="inbound"` lists received
  mail / replies; this is the filter that closes the blind spot.
- `get_email` returns `body_text` / `body_html`, so the agent reads reply
  content directly. No raw/attachment tools — calls don't expose recording
  fetch via MCP either; keeping the surfaces symmetric.

## Changes (all within `mcp/`)

The REST endpoints already exist; this is MCP wiring only.

1. **`mcp/hailhq/mcp/hail_client.py`** — add `EmailListResponse` to the
   existing `hailhq.core.schemas` import; add `get_email(email_id)` and
   `list_emails(...)` GET methods mirroring `get_call` / `list_calls`
   (current lines 127–153), validating responses against `EmailResponse` /
   `EmailListResponse`.

2. **`mcp/hailhq/mcp/tools.py`** — add `get_email` and `list_emails` domain
   functions with the same `try/except ValidationError / HailAPIError` shape
   as `get_call` / `list_calls`; register two FastMCP closures with full
   docstrings (the docstring is the agent's only documentation — put the
   `direction="inbound"` hint there). Update the module docstring
   ("five tools" → "seven") and `__all__`.

3. **`mcp/tests/`** — unit tests calling the domain functions with a
   constructed `HailClient` (the file's stated test pattern), including the
   404 → `resource not found` error-mapping case.

## Out of scope (explicit)

- API / SDK / CLI / OpenAPI / core: untouched.
- No cancel verbs.
- No email event stream and no `get_events` for email. (`/events` is
  `CallEvent`-backed; `SUPPORTED_RESOURCE_TYPES = ("call",)`. Email events are
  built transiently for webhook fan-out and never persisted to a queryable
  store, so `get_events(id="email:…")` would require a new event table +
  migration + API work. Tracked as a separate future spec.)
- No raw / attachment tools.

## Testing & verification

- `uv run pytest` green in `mcp/`; mypy / ruff / black clean.
- Manual smoke: `list_emails(direction="inbound")` against the running MCP
  returns received mail; `get_email(<id>)` returns its bodies.
