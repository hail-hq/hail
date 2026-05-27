# MCP Contract-Driven Tool Layer (Phase 0b) — Design

- **Date**: 2026-05-27
- **Status**: Approved design (brainstorming output).
- **Roadmap**: Phase 0b of [2026-05-26-mcp-server-roadmap-design.md](2026-05-26-mcp-server-roadmap-design.md).
- **Scope**: The `mcp/` service only. No API, transport, or auth changes. No user-facing behavior change.

## Goal

Stop the MCP tool layer from hand-re-encoding the API's wire contract and validation rules. Make it consume the **same `core/hailhq/core/schemas.py` Pydantic models the API uses**, so a contract change either surfaces automatically or fails type-checking — drift becomes impossible by construction — and the duplicated validation in `tools.py` is deleted.

## Why share `core/` models (not codegen)

`core/schemas.py` already defines the contract (`CallCreate`, `EmailCreate`, `CallResponse`, `CallListResponse`, `EmailResponse`, `EventStreamResponse`, `LLMConfig`); the API routes import them and FastAPI generates `openapi/openapi.yaml` _from_ them. The MCP service is Python and already imports `hailhq.core.schemas` (`tools.py` uses `parse_resource_id`). So importing the models is consuming the source of truth directly.

- **OpenAPI → Python codegen** — rejected: a redundant toolchain for a consumer that can import the source. (The Go CLI codegens only because it cannot import Python models. `openapi.yaml` remains the source of truth _for the CLI_; this is unchanged.)
- **Keep hand-written + add a contract test** — rejected: still duplicates logic; detects drift later instead of preventing it.

## Current state (what's duplicated today)

`mcp/hailhq/mcp/hail_client.py` builds request bodies as hand-assembled dicts and returns raw `dict[str, Any]`. `mcp/hailhq/mcp/tools.py` re-implements validation the core models already enforce:

| Rule                                        | Hand-coded in tools.py today | Already in core model                            |
| ------------------------------------------- | ---------------------------- | ------------------------------------------------ |
| system_prompt XOR llm                       | yes                          | `CallCreate._prompt_or_llm`                      |
| E.164 for `to`/`from`                       | yes (hints + checks)         | `CallCreate._validate_e164`                      |
| `llm` completeness (base_url/api_key/model) | yes (partial-llm check)      | `LLMConfig` (required fields + `extra="forbid"`) |
| ≥1 recipient                                | yes                          | `EmailCreate.to` (`min_length=1`)                |
| body_text or body_html                      | yes                          | `EmailCreate._body_required`                     |

## Design

### `hail_client.py` — model-driven wire layer

Each method builds the request from the matching core model, then serializes:

- **`place_call`** → `CallCreate`; **`send_email`** → `EmailCreate`.
- Build a dict of **only the provided (non-`None`) fields, keyed by wire/alias name**, and construct via `Model.model_validate(fields)`. Use alias keys (`"from"`, not `"from_"`): `CallCreate` sets `extra="forbid"` and does **not** set `populate_by_name`, so it must be populated by alias — `CallCreate(from_=...)` would raise. (`EmailCreate` sets `populate_by_name=True`, so it tolerates either, but use alias keys uniformly.) Keeping only provided fields in the input dict means `exclude_unset` on dump reproduces today's minimal wire body (no defaults like `voice_config`/`metadata` leak in).
- Serialize the body with `model.model_dump(mode="json", by_alias=True, exclude_unset=True)`.
  - `by_alias=True` emits `from` (the `from_` field's alias), matching the API.
  - `exclude_unset=True` sends only fields the agent actually passed — preserves the current minimal wire shape.
- Model construction runs the API's validators **locally, before the HTTP call** (same short-circuit as today). A `pydantic.ValidationError` propagates to the tool layer, which maps it (see below) — no request is sent.
- The `llm` argument stays a plain dict at the tool boundary; `CallCreate(llm=<dict>)` coerces it to `LLMConfig`, which validates completeness.

Response handling:

- On 2xx, parse the JSON into the matching core response model and return `model.model_dump(mode="json")` (no `by_alias` — response field names are already the wire names; this matches what the API emits):
  - `place_call` → `CallResponse`
  - `get_call` → `CallResponse`
  - `list_calls` → `CallListResponse`
  - `send_email` → `EmailResponse`
  - `get_events` → `EventStreamResponse`
- Non-2xx still maps to `HailAPIError(status, detail)` via the existing `_decode` error path (unchanged).

Idempotency-Key handling is unchanged (it is a request header, not a body field; the auto-UUID + passthrough logic stays).

### `tools.py` — pure agent surface

- **Delete** the hand-written validation listed in the table above; it is now enforced by `CallCreate` / `EmailCreate` / `LLMConfig` during construction in `hail_client`.
- **Keep** the curated agent-facing signatures and rich docstrings (the part tuned for agents), the Idempotency-Key surfacing in the response, and `parse_resource_id` for the `get_events` `id=call:<uuid>` filter (a query-param concern, not part of the request bodies).
- Add a small helper, `_validation_error_message(exc: ValidationError) -> str`, returning the first error's `msg` (optionally prefixed with the field name). Wrap the `hail_client` call so `ValidationError` → `{"error": <message>}`. The core models' messages are already agent-friendly:
  - "system_prompt and llm are mutually exclusive (use one mode)"
  - "either system_prompt or llm must be provided"
  - "either body_text or body_html must be provided"
  - "must be E.164 (e.g. +14155551234)"

### Boundaries

- `hail_client.py`: typed wire layer over core models + httpx. One responsibility — translate tool calls to/from the API using the shared contract.
- `tools.py`: agent-facing surface — signatures, docstrings, error mapping. No contract knowledge beyond the curated parameters.
- **Drift guard is structural** (MCP imports the same models the API uses) plus mypy. No codegen, no sync step, no separate contract test required.

## Tests (`mcp/tests/test_tools.py`)

- **Update validation-error assertions** to the core models' messages. Known change: `test_place_call_rejects_neither_mode` asserts "must provide either" today → core says "either system_prompt or llm must be provided"; update to assert on "either system_prompt or llm". `test_place_call_rejects_both_modes` ("mutually exclusive") still matches. Align the E.164, partial-`llm`, empty-recipient, and missing-body assertions to the core messages.
- **Keep unchanged**: the `from`-alias wire test, the minimal-happy-path body assertions (still minimal via `exclude_unset`), the Idempotency-Key tests, and all `HailAPIError` mapping tests (401/404/409/422/503/5xx).
- **Response parsing**: the existing mock bodies (`_call_response()`, `_email_response()`) are already full-shaped and satisfy the response models; assertions on returned dict fields continue to hold.
- All tests must pass; run `cd mcp && uv run pytest -q`. mypy must stay clean.

## Out of scope (remains Phase 2)

- Typed / structured tool **output schemas** (returning models as MCP structured content rather than `dict`).
- `readOnlyHint` / `destructiveHint` tool annotations.

0b is request construction + response parsing + validation dedup only.

## Done criteria

- `hail_client.py` builds every request via a core request model and parses every 2xx response via a core response model; no hand-assembled request dicts remain.
- `tools.py` contains no duplicated validation rules; all field/shape validation comes from the core models.
- Existing tool behavior is unchanged on the wire (minimal bodies, `from` alias, idempotency); error messages come from the core models.
- Full suite green; mypy clean.
