# MCP Contract-Driven Tool Layer (Phase 0b) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the MCP tool layer consume the API's own `core/hailhq/core/schemas.py` Pydantic models for requests and responses, so the wire contract and its validation rules live in one place (no drift) and the duplicated validation in `tools.py` is deleted.

**Architecture:** `hail_client.py` builds each request by constructing the matching core request model (`CallCreate`/`EmailCreate`) — which runs the API's validators locally — and serializes it; it parses each 2xx response through the matching core response model. `tools.py` drops its hand-written validation and maps `pydantic.ValidationError` to the agent-facing `{"error": …}` dict. No API, transport, or auth change; the wire shape is unchanged.

**Tech Stack:** Python 3.11, Pydantic v2 (`hailhq.core.schemas`), httpx, pytest (`asyncio_mode = auto`) + respx.

**Spec:** [2026-05-27-mcp-contract-driven-tools-design.md](../specs/2026-05-27-mcp-contract-driven-tools-design.md).

---

## Background the implementer needs

- The MCP tool functions are module-level in `mcp/hailhq/mcp/tools.py` (`place_call`, `send_email`, `get_call`, `list_calls`, `get_events`); unit tests call them directly with a constructed `HailClient`. `register_tools(...)` (further down the file) wraps them for FastMCP and **does not change** in this plan.
- `core/hailhq/core/schemas.py` already defines: `CallCreate` (validators: `_validate_e164`, `_prompt_or_llm`), `LLMConfig` (`extra="forbid"`, required `base_url/api_key/model`), `CallResponse`, `CallListResponse`, `EmailCreate` (validators: email-format, `_body_required`, `to` `min_length=1`), `EmailResponse`, `EventStreamResponse`.
- **Alias gotcha:** `CallCreate` aliases `from_`→`from` and does **not** set `populate_by_name`, so it must be populated with the alias key `"from"` (via `model_validate({"from": ...})`), not `CallCreate(from_=...)`. `EmailCreate` sets `populate_by_name=True`. The plan uses alias keys + `model_validate` uniformly.
- Pydantic v2 wraps a validator's `ValueError` as an error whose `msg` is prefixed `"Value error, "`; model-level (`model_validator`) errors have an empty `loc`, field errors have a `loc` like `("llm", "model")`.

## File Structure

- **Modify** `mcp/hailhq/mcp/hail_client.py` — request building + response parsing become model-driven. Single responsibility: typed wire layer over the core models + httpx.
- **Modify** `mcp/hailhq/mcp/tools.py` — delete duplicated validation; map `ValidationError`. Single responsibility: agent-facing surface (signatures, docstrings, error mapping).
- **Modify** `mcp/tests/test_tools.py` — update the two validation-error assertions whose message intentionally changes.

---

### Task 1: Make `hail_client.py` model-driven

**Files:**

- Modify: `mcp/hailhq/mcp/hail_client.py`

This task is behavior-preserving: `tools.py` still runs its own validation, so the suite stays green. We only change _how_ the request body is built (via models) and parse responses through models.

- [ ] **Step 1: Replace the entire contents of `mcp/hailhq/mcp/hail_client.py` with:**

```python
"""Thin async httpx wrapper around the Hail API.

The MCP service talks to the same public ``POST /calls`` / ``POST /emails``
/ ``GET /calls`` / ``GET /events`` surface external clients use. Request
bodies are built from the *shared* ``hailhq.core.schemas`` models the API
itself uses, and 2xx responses are parsed through the matching response
model — so the wire contract (field names, aliases, validation) lives in
exactly one place and cannot drift from the API.

* ``Authorization: Bearer <hail_api_key>`` is auto-injected on every request.
* ``Idempotency-Key`` is auto-injected on ``place_call`` / ``send_email``
  (a fresh UUID per invocation unless the caller passed one).
* Non-2xx responses map to :class:`HailAPIError`; the tool layer turns that
  into a structured ``{"error": ...}`` payload. A request model that fails
  validation raises ``pydantic.ValidationError`` *before* any HTTP call —
  the tool layer maps that too.

Configuration reads from :data:`hailhq.core.config.settings`; constructor
kwargs override for tests.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from hailhq.core.config import settings
from hailhq.core.schemas import (
    CallCreate,
    CallListResponse,
    CallResponse,
    EmailCreate,
    EmailResponse,
    EventStreamResponse,
)


class HailAPIError(Exception):
    """Non-2xx response from the Hail API.

    ``status`` is the HTTP status code; ``detail`` is the parsed ``detail``
    field from the JSON body when present, otherwise the raw response text.
    The MCP tool layer converts this to an agent-facing error dict.
    """

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"hail api error {status}: {detail}")
        self.status = status
        self.detail = detail


class HailClient:
    """Async httpx client for the Hail API."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = (base_url or settings.hail_api_url).rstrip("/")
        self._api_key = api_key if api_key is not None else settings.hail_api_key
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {self._api_key}"},
        )

    async def __aenter__(self) -> "HailClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ #
    # POST /calls
    # ------------------------------------------------------------------ #

    async def place_call(
        self,
        *,
        to: str,
        system_prompt: str | None = None,
        llm: dict[str, Any] | None = None,
        from_: str | None = None,
        first_message: str | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """POST /calls — originate an outbound call.

        Builds the body from :class:`CallCreate` (which enforces E.164,
        system_prompt-XOR-llm, and ``LLMConfig`` completeness). Construction
        raises ``pydantic.ValidationError`` before any HTTP on bad input.
        """
        fields: dict[str, Any] = {"to": to}
        if from_ is not None:
            fields["from"] = from_  # alias key — CallCreate has no populate_by_name
        if system_prompt is not None:
            fields["system_prompt"] = system_prompt
        if llm is not None:
            fields["llm"] = llm
        if first_message is not None:
            fields["first_message"] = first_message
        if metadata is not None:
            fields["metadata"] = metadata

        body = CallCreate.model_validate(fields).model_dump(
            mode="json", by_alias=True, exclude_unset=True
        )
        headers = {"Idempotency-Key": idempotency_key or str(uuid.uuid4())}
        resp = await self._client.post("/calls", json=body, headers=headers)
        return CallResponse.model_validate(_decode(resp)).model_dump(mode="json")

    # ------------------------------------------------------------------ #
    # GET /calls/{id}
    # ------------------------------------------------------------------ #

    async def get_call(self, call_id: str) -> dict[str, Any]:
        resp = await self._client.get(f"/calls/{call_id}")
        return CallResponse.model_validate(_decode(resp)).model_dump(mode="json")

    # ------------------------------------------------------------------ #
    # GET /calls
    # ------------------------------------------------------------------ #

    async def list_calls(
        self,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        status: str | None = None,
        to: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if cursor is not None:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = limit
        if status is not None:
            params["status"] = status
        if to is not None:
            params["to"] = to
        resp = await self._client.get("/calls", params=params)
        return CallListResponse.model_validate(_decode(resp)).model_dump(mode="json")

    # ------------------------------------------------------------------ #
    # POST /emails
    # ------------------------------------------------------------------ #

    async def send_email(
        self,
        *,
        to: list[str],
        subject: str,
        body_text: str | None = None,
        body_html: str | None = None,
        from_: str | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """POST /emails — send an outbound message.

        Builds the body from :class:`EmailCreate` (which enforces ≥1
        recipient, a non-empty subject, body-required, and email formats).
        """
        fields: dict[str, Any] = {"to": list(to), "subject": subject}
        if from_ is not None:
            fields["from"] = from_
        if body_text is not None:
            fields["body_text"] = body_text
        if body_html is not None:
            fields["body_html"] = body_html
        if cc:
            fields["cc"] = list(cc)
        if bcc:
            fields["bcc"] = list(bcc)
        if reply_to is not None:
            fields["reply_to"] = reply_to
        if metadata is not None:
            fields["metadata"] = metadata

        body = EmailCreate.model_validate(fields).model_dump(
            mode="json", by_alias=True, exclude_unset=True
        )
        headers = {"Idempotency-Key": idempotency_key or str(uuid.uuid4())}
        resp = await self._client.post("/emails", json=body, headers=headers)
        return EmailResponse.model_validate(_decode(resp)).model_dump(mode="json")

    # ------------------------------------------------------------------ #
    # GET /events
    # ------------------------------------------------------------------ #

    async def get_events(
        self,
        *,
        id: str | None = None,
        kind: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if id is not None:
            params["id"] = id
        if kind is not None:
            params["kind"] = kind
        if cursor is not None:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = limit
        resp = await self._client.get("/events", params=params)
        return EventStreamResponse.model_validate(_decode(resp)).model_dump(mode="json")


def _decode(resp: httpx.Response) -> Any:
    """Return the JSON body on 2xx, raise :class:`HailAPIError` otherwise."""
    if 200 <= resp.status_code < 300:
        return resp.json()
    detail: str
    try:
        payload = resp.json()
    except ValueError:
        detail = resp.text or resp.reason_phrase
    else:
        if isinstance(payload, dict) and "detail" in payload:
            d = payload["detail"]
            detail = d if isinstance(d, str) else str(d)
        else:
            detail = str(payload)
    raise HailAPIError(status=resp.status_code, detail=detail)


__all__ = ["HailAPIError", "HailClient"]
```

- [ ] **Step 2: Run the full suite — it must stay green**

Run: `cd mcp && uv run pytest -q`
Expected: PASS (all current tests). Rationale: `tools.py` still runs its own validation, so error-message assertions are unaffected; the happy-path body assertions still hold because `model_dump(by_alias=True, exclude_unset=True)` reproduces the minimal body (e.g. `{"to": ..., "system_prompt": ...}` and `body["from"]` for the alias test), and the response mocks (`_call_response()`, `_email_response()`) satisfy the response models.

- [ ] **Step 3: Typecheck**

Run: `cd mcp && uv run mypy --namespace-packages --explicit-package-bases hailhq/mcp/hail_client.py`
Expected: `Success: no issues found`.

- [ ] **Step 4: Commit**

```bash
git add mcp/hailhq/mcp/hail_client.py
git commit -m "$(printf 'refactor(mcp): build requests and parse responses via core schemas\n\nhail_client now constructs CallCreate/EmailCreate for request bodies and\nparses 2xx responses through CallResponse/CallListResponse/EmailResponse/\nEventStreamResponse, sharing the API contract instead of hand-encoding it.\nNo wire-shape change.\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>')"
```

---

### Task 2: Delete duplicated validation in `tools.py` and map `ValidationError`

**Files:**

- Modify: `mcp/hailhq/mcp/tools.py`
- Modify: `mcp/tests/test_tools.py`

Now that the core models validate during request construction (Task 1), `tools.py`'s hand-written checks are redundant. Removing them changes two error _messages_ to the core models' wording, so two test assertions update in the same task to keep the suite green.

- [ ] **Step 1: Update the module docstring and imports.**

In `mcp/hailhq/mcp/tools.py`, replace the paragraph in the module docstring that begins `"Errors are returned as ..."` and mentions `"Validation that can be done locally (mode A/B exclusivity, ``<type>:<uuid>`` shape) runs before any HTTP ..."` with:

```
Errors are returned as ``{"error": "<message>"}`` dicts rather than raised
— agents read tool responses, not exception traces. Field/shape validation
lives in the shared ``hailhq.core.schemas`` request models (constructed in
``hail_client``); a ``pydantic.ValidationError`` is mapped to an ``{"error":
...}`` dict here. The ``<type>:<uuid>`` resource-id shape for ``get_events``
is checked locally with ``parse_resource_id``.
```

Add the Pydantic import next to the existing imports (after `from typing import Any`):

```python
from pydantic import ValidationError
```

- [ ] **Step 2: Delete the duplicated-validation block.**

Remove the entire section from the comment `# Mode validation — mirrors cli/internal/cmd/call.go validateMode().` through the end of the `_validate_modes` function — i.e. delete `_LLM_REQUIRED_KEYS` and `_validate_modes` (the block spanning the original lines ~55–94). Keep `_format_api_error` above it.

- [ ] **Step 3: Add the validation-error formatter.** Immediately after `_format_api_error`, add:

```python
def _validation_error_message(exc: ValidationError) -> str:
    """First Pydantic error as a compact agent-facing string.

    Field errors read as ``loc: msg`` (e.g. ``llm.model: Field required``);
    model-level errors (mode A/B, body-required) have an empty loc, so just
    the message. The text comes from ``hailhq.core.schemas`` — the single
    source of the contract — so this layer never restates a rule.
    """
    err = exc.errors()[0]
    loc = ".".join(str(p) for p in err["loc"])
    msg = str(err["msg"])
    return f"{loc}: {msg}" if loc else msg
```

- [ ] **Step 4: Replace the five tool functions** (`place_call` through `get_events`) with these versions (validation removed; `ValidationError` caught):

```python
async def place_call(
    *,
    client: HailClient,
    to: str,
    system_prompt: str | None = None,
    llm: dict[str, Any] | None = None,
    from_: str | None = None,
    first_message: str | None = None,
    metadata: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if idempotency_key is None:
        idempotency_key = str(uuid.uuid4())
    try:
        result = await client.place_call(
            to=to,
            system_prompt=system_prompt,
            llm=llm,
            from_=from_,
            first_message=first_message,
            metadata=metadata,
            idempotency_key=idempotency_key,
        )
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
    except HailAPIError as exc:
        return _format_api_error(exc)
    # Surface the key so the agent can replay this exact request on a retry.
    if isinstance(result, dict):
        result.setdefault("idempotency_key", idempotency_key)
    return result


async def send_email(
    *,
    client: HailClient,
    to: list[str],
    subject: str,
    body_text: str | None = None,
    body_html: str | None = None,
    from_: str | None = None,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    reply_to: str | None = None,
    metadata: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if idempotency_key is None:
        idempotency_key = str(uuid.uuid4())
    try:
        result = await client.send_email(
            to=to,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            from_=from_,
            cc=cc,
            bcc=bcc,
            reply_to=reply_to,
            metadata=metadata,
            idempotency_key=idempotency_key,
        )
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
    except HailAPIError as exc:
        return _format_api_error(exc)
    if isinstance(result, dict):
        result.setdefault("idempotency_key", idempotency_key)
    return result


async def get_call(*, client: HailClient, call_id: str) -> dict[str, Any]:
    try:
        return await client.get_call(call_id)
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
    except HailAPIError as exc:
        return _format_api_error(exc)


async def list_calls(
    *,
    client: HailClient,
    cursor: str | None = None,
    limit: int = 50,
    status: str | None = None,
    to: str | None = None,
) -> dict[str, Any]:
    try:
        return await client.list_calls(cursor=cursor, limit=limit, status=status, to=to)
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
    except HailAPIError as exc:
        return _format_api_error(exc)


async def get_events(
    *,
    client: HailClient,
    id: str | None = None,
    kind: str | None = None,
    cursor: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    if id is not None:
        try:
            parse_resource_id(id)
        except ValueError as exc:
            return {"error": str(exc)}
    try:
        return await client.get_events(id=id, kind=kind, cursor=cursor, limit=limit)
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
    except HailAPIError as exc:
        return _format_api_error(exc)
```

Leave the agent-facing docstrings inside `register_tools`' wrappers and the `register_tools` function itself unchanged.

- [ ] **Step 5: Update the two test assertions whose message changed.**

In `mcp/tests/test_tools.py`, in `test_place_call_rejects_neither_mode`, change:

```python
    assert "must provide either" in result["error"]
```

to:

```python
    assert "either system_prompt or llm" in result["error"]
```

In `test_send_email_rejects_empty_recipients`, change:

```python
    result = await tools.send_email(
        client=client, to=[], subject="hi", body_text="body"
    )
    assert result == {"error": "to must contain at least one recipient"}
    # respx records every call; verify no HTTP went out.
    assert not respx.calls.called
```

to:

```python
    result = await tools.send_email(
        client=client, to=[], subject="hi", body_text="body"
    )
    # EmailCreate.to has min_length=1; the message now comes from the model.
    assert "error" in result
    assert "at least 1" in result["error"]
    # respx records every call; verify no HTTP went out.
    assert not respx.calls.called
```

Leave all other tests unchanged — `test_place_call_rejects_both_modes` ("mutually exclusive"), `test_send_email_requires_a_body` ("body_text or body_html"), and `test_place_call_llm_validation_rejects_partial` ("model", now surfaced as `llm.model: ...`) still match the core models' messages; the alias/wire and idempotency and `HailAPIError`-mapping tests are unaffected.

- [ ] **Step 6: Run the full suite**

Run: `cd mcp && uv run pytest -q`
Expected: PASS (all tests). If `test_place_call_rejects_both_modes` or `test_send_email_requires_a_body` fail, the core message wording differs from the spec's assumption — read the actual `result["error"]` and align the assertion to the real `hailhq.core.schemas` message (do not re-add validation to `tools.py`).

- [ ] **Step 7: Lint + typecheck**

Run: `cd mcp && uv run ruff check hailhq/mcp/tools.py tests/test_tools.py && uv run mypy --namespace-packages --explicit-package-bases hailhq/mcp/tools.py`
Expected: ruff "All checks passed!"; mypy "Success".

- [ ] **Step 8: Commit**

```bash
git add mcp/hailhq/mcp/tools.py mcp/tests/test_tools.py
git commit -m "$(printf 'refactor(mcp): delete duplicated validation, defer to core schemas\n\ntools.py no longer re-implements mode-A/B, E.164, recipient, body, or llm\nvalidation; those come from CallCreate/EmailCreate/LLMConfig via\nhail_client. A pydantic ValidationError maps to {error: msg}. Two test\nassertions track the core models messages.\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>')"
```

---

## Self-Review

- **Spec coverage:** request build via core models (Task 1, `place_call`/`send_email` use `CallCreate`/`EmailCreate`, alias keys + `model_validate`, `by_alias`+`exclude_unset`) ✓; response parse via core models (Task 1, all five methods) ✓; delete duplicated validation (Task 2 Step 2) ✓; map `ValidationError` (Task 2 Steps 3–4) ✓; update test assertions (Task 2 Step 5) ✓; idempotency unchanged (kept in both tool funcs + client) ✓; `parse_resource_id` kept (Task 2 Step 4 `get_events`) ✓; no wire-shape change (Task 1 Step 2 rationale) ✓.
- **Placeholder scan:** none — full file content in Task 1, full function bodies + exact before/after diffs in Task 2.
- **Type/name consistency:** `_validation_error_message(exc: ValidationError) -> str` defined once (Step 3) and called in all five functions (Step 4); `_decode` return type widened to `Any` (it now feeds `model_validate`); response models match the spec's per-method mapping; `register_tools` untouched so the tool wrappers still resolve these module functions.
- **Out of scope (Phase 2), not added:** structured output schemas, tool annotations.
