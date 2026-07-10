# Disclosure Line Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Blend the email branding footer and AI disclosure into one line, tighten the voice fallback disclosure, and speak the actual organization name on voice calls (resolved via a fail-safe live lookup from hail-website) to close a TCPA identity-disclosure gap.

**Architecture:** Email is a pure string change in `core/hailhq/core/email_footer.py` plus its one call site. Voice adds a signed hail→hail-website internal lookup (`fetch_organization_name`, 1000ms timeout, `None` on any failure) called from `POST /calls` before dial-out; the name rides the existing LiveKit dispatch-metadata dict to the voicebot, which interpolates it into the hardcoded `session.say()` disclosure — falling back to generic wording whenever the name is absent.

**Tech Stack:** Python (FastAPI, aiohttp, pytest) in `hail/`; TypeScript (Next.js route handler, vitest, `pg`) in `hail-website/`.

**Spec:** `docs/superpowers/specs/2026-07-08-disclosure-line-improvements-design.md`

## Global Constraints

- Two git repos are involved. Tasks 1, 2, 4, 5, 6 commit in `/Users/r/playground/hail`; Task 3 commits in `/Users/r/playground/hail-website`. Never mix.
- Email sent-footer copy, verbatim: text body `Sent via Hail.so (https://hail.so), an AI communication platform.` — HTML body `Sent via <a href="https://hail.so">Hail.so</a>, an AI communication platform.` (hyperlinked in HTML, plain in text).
- Voice generic fallback line, verbatim: `Hi, this is an AI assistant calling on behalf of whoever requested this call.`
- Voice named line, verbatim template: `Hi, this is an AI assistant calling on behalf of {org_name}.`
- Org-name lookup: 1000ms total timeout, exactly one attempt, no retries. Any failure (unset `HAIL_BASE_URL`/`HAIL_INTERNAL_SECRET`, timeout, non-200, connection error, malformed body, blank name) → `None`. Never raises into the call path.
- The email **forwarding** footer (`FOOTER_FORWARDED`, `core/hailhq/core/email_forwarding.py`) is unchanged — forwards are not AI-generated content.
- Repo invariant (hail `CLAUDE.md`): URLs are built with `hailhq.core.urls.join_url`, never f-strings or ad-hoc `rstrip("/")`.
- Python: run `uv run pytest -q` in the touched package dir; `uv run ruff check` on touched files. Website: `npm test` (vitest).
- Conventional Commits.

---

### Task 1: Email footer — single blended line (core)

**Files:**

- Modify: `core/hailhq/core/email_footer.py`
- Test: `core/tests/test_email_footer.py`

**Interfaces:**

- Consumes: nothing new.
- Produces: `SENT_FOOTER_TEXT: str` (the exact text-body line) and `append_sent_footer(body_text: str | None, body_html: str | None) -> tuple[str | None, str | None]`. Removes `FOOTER_SENT`, `AI_DISCLOSURE_LINE`, `append_disclosure` from this module (Task 2 fixes the only external call site). `append_footer(body_text, body_html, *, label)` and `FOOTER_FORWARDED` remain unchanged for the forwarding path.

- [ ] **Step 1: Rewrite the test file with the new expectations**

Replace the entire contents of `core/tests/test_email_footer.py` with:

```python
from hailhq.core.email_footer import (
    FOOTER_FORWARDED,
    SENT_FOOTER_TEXT,
    append_footer,
    append_sent_footer,
)


def test_append_sent_footer_is_one_blended_line():
    text, html = append_sent_footer("hello", "<p>hello</p>")
    assert text == (
        "hello\n\n--\nSent via Hail.so (https://hail.so), "
        "an AI communication platform."
    )
    assert html is not None
    assert html.startswith("<p>hello</p>")
    assert 'href="https://hail.so"' in html
    # One footer paragraph — not a branding footer plus a separate
    # disclosure paragraph (the pre-2026-07 layout this replaced).
    assert html.count("an AI communication platform") == 1


def test_append_sent_footer_none_parts_stay_none():
    text, html = append_sent_footer(None, "<p>x</p>")
    assert text is None
    assert html is not None and "an AI communication platform" in html

    text, html = append_sent_footer("x", None)
    assert html is None
    assert text is not None and "an AI communication platform" in text


def test_append_footer_forwarded_label_unchanged():
    text, html = append_footer("hello", "<p>hello</p>", label=FOOTER_FORWARDED)
    assert text == "hello\n\n--\nForwarded by Hail.so (https://hail.so)"
    assert html is not None
    assert "Forwarded by Hail.so" in html
    assert 'href="https://hail.so"' in html


def test_sent_footer_text_constant_is_the_wire_line():
    assert SENT_FOOTER_TEXT == (
        "Sent via Hail.so (https://hail.so), an AI communication platform."
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/r/playground/hail/core && uv run pytest -q tests/test_email_footer.py`
Expected: FAIL — `ImportError: cannot import name 'SENT_FOOTER_TEXT'`

- [ ] **Step 3: Rewrite the module**

Replace the entire contents of `core/hailhq/core/email_footer.py` with:

```python
"""Branding footer appended to every outbound message.

Applied at the send boundary for direct sends (the stored Email row keeps
the tenant-authored body; the wire message carries the footer) and at
build time for forwards (the queued row already holds the final body).

Direct sends get one blended line that is both branding and AI
disclosure ("Sent via Hail.so, an AI communication platform.") — kept as
a single sentence deliberately so it reads as attribution, not a legal
disclaimer, while still disclosing AI involvement. Forwards keep the
plain "Forwarded by Hail.so" label: forwarded mail is a relayed human
message, not AI-generated content, so no AI disclosure applies.
"""

from __future__ import annotations

__all__ = [
    "FOOTER_FORWARDED",
    "SENT_FOOTER_TEXT",
    "append_footer",
    "append_sent_footer",
]

_LINK = "https://hail.so"

FOOTER_FORWARDED = "Forwarded by Hail.so"

# The single blended branding + AI-disclosure line for direct sends.
# Text form carries the literal URL; the HTML form hyperlinks "Hail.so".
SENT_FOOTER_TEXT = (
    f"Sent via Hail.so ({_LINK}), an AI communication platform."
)
_SENT_FOOTER_HTML = (
    '<p style="margin-top:16px;font-size:12px;color:#8a8a8a;">'
    f'--<br>Sent via <a href="{_LINK}">Hail.so</a>, '
    "an AI communication platform.</p>"
)


def _text_footer(label: str) -> str:
    return f"\n\n--\n{label} ({_LINK})"


def _html_footer(label: str) -> str:
    return (
        '<p style="margin-top:16px;font-size:12px;color:#8a8a8a;">'
        f'--<br>{label} (<a href="{_LINK}">hail.so</a>)</p>'
    )


def append_footer(
    body_text: str | None, body_html: str | None, *, label: str
) -> tuple[str | None, str | None]:
    """Append the labeled branding footer (forwards) to whichever parts exist."""
    if body_text is not None:
        body_text = body_text + _text_footer(label)
    if body_html is not None:
        body_html = body_html + _html_footer(label)
    return body_text, body_html


def append_sent_footer(
    body_text: str | None, body_html: str | None
) -> tuple[str | None, str | None]:
    """Append the blended branding + AI-disclosure line for direct sends.

    Applied at the send boundary so the line always rides the wire
    message, never the stored row.
    """
    if body_text is not None:
        body_text = body_text + f"\n\n--\n{SENT_FOOTER_TEXT}"
    if body_html is not None:
        body_html = body_html + _SENT_FOOTER_HTML
    return body_text, body_html
```

- [ ] **Step 4: Run core tests**

Run: `cd /Users/r/playground/hail/core && uv run pytest -q tests/test_email_footer.py tests/test_email_forwarding.py`
Expected: PASS (footer tests green; forwarding path untouched and still green). If `tests/test_email_forwarding.py` doesn't exist under that exact name, run `uv run pytest -q` for the whole package instead.

- [ ] **Step 5: Lint and commit (do NOT run the api suite yet — Task 2 fixes its call site)**

```bash
cd /Users/r/playground/hail/core && uv run ruff check hailhq/core/email_footer.py tests/test_email_footer.py
cd /Users/r/playground/hail
git add core/hailhq/core/email_footer.py core/tests/test_email_footer.py
git commit -m "feat(core): blend email branding footer and AI disclosure into one line"
```

---

### Task 2: Email footer — wire the send route to the new single line (api)

**Files:**

- Modify: `api/hailhq/api/routes/emails.py:55` (import) and `api/hailhq/api/routes/emails.py:383-388` (call site)
- Test: `api/tests/test_emails_api.py:14` (import) and `api/tests/test_emails_api.py:1082-1094` (assertions)

**Interfaces:**

- Consumes: `append_sent_footer` from Task 1.
- Produces: nothing new — wire behavior only.

- [ ] **Step 1: Update the test assertions to expect the blended line**

In `api/tests/test_emails_api.py`, change line 14 from:

```python
from hailhq.core.email_footer import AI_DISCLOSURE_LINE
```

to:

```python
from hailhq.core.email_footer import SENT_FOOTER_TEXT
```

Then find this block (~lines 1082-1094):

```python
    call_kwargs = email_mock.send_email.call_args.kwargs
    assert call_kwargs["body_text"].startswith("body")
    assert "Sent by Hail.so" in call_kwargs["body_text"]
    assert call_kwargs["body_html"].startswith("<p>body</p>")
    assert 'href="https://hail.so"' in call_kwargs["body_html"]
    # AI disclosure rides the wire message too, after the branding footer —
    # never part of the stored/returned body (see assertions below).
    assert AI_DISCLOSURE_LINE in call_kwargs["body_text"]
    assert AI_DISCLOSURE_LINE in call_kwargs["body_html"]
    assert call_kwargs["body_text"].index("Sent by Hail.so") < call_kwargs[
        "body_text"
    ].index(AI_DISCLOSURE_LINE)
```

and replace it with:

```python
    call_kwargs = email_mock.send_email.call_args.kwargs
    assert call_kwargs["body_text"].startswith("body")
    assert SENT_FOOTER_TEXT in call_kwargs["body_text"]
    assert call_kwargs["body_html"].startswith("<p>body</p>")
    assert 'href="https://hail.so"' in call_kwargs["body_html"]
    # Branding + AI disclosure are one blended footer line on the wire
    # message — never part of the stored/returned body (see below).
    assert "an AI communication platform" in call_kwargs["body_html"]
    assert "Sent by Hail.so" not in call_kwargs["body_text"]
```

- [ ] **Step 2: Run the touched test to verify it fails**

Run: `cd /Users/r/playground/hail/api && uv run pytest -q tests/test_emails_api.py -k "footer or disclosure"`
Expected: FAIL (the route still emits the old two-paragraph layout; also the old import may error until Step 3). If `-k` matches nothing, run the specific test containing the block above (find it with `grep -n "SENT_FOOTER_TEXT" tests/test_emails_api.py` and run that test by name).

- [ ] **Step 3: Update the route**

In `api/hailhq/api/routes/emails.py`, change line 55 from:

```python
from hailhq.core.email_footer import FOOTER_SENT, append_disclosure, append_footer
```

to:

```python
from hailhq.core.email_footer import append_sent_footer
```

and change the call site (~lines 383-388) from:

```python
    # Branding footer + AI disclosure ride the wire message only; the stored
    # row keeps the tenant-authored body.
    wire_text, wire_html = append_footer(
        email.body_text, email.body_html, label=FOOTER_SENT
    )
    wire_text, wire_html = append_disclosure(wire_text, wire_html)
```

to:

```python
    # Blended branding + AI-disclosure footer rides the wire message only;
    # the stored row keeps the tenant-authored body.
    wire_text, wire_html = append_sent_footer(email.body_text, email.body_html)
```

- [ ] **Step 4: Run the full api suite**

Run: `cd /Users/r/playground/hail/api && uv run pytest -q`
Expected: PASS (276+ tests).

- [ ] **Step 5: Verify no stragglers reference the removed names**

Run: `cd /Users/r/playground/hail && grep -rn "FOOTER_SENT\|append_disclosure\|Sent by Hail" --include="*.py" api core voicebot mcp | grep -v __pycache__`
Expected: no output. If anything appears, update it to the new names/copy before committing.

- [ ] **Step 6: Lint and commit**

```bash
cd /Users/r/playground/hail/api && uv run ruff check hailhq/api/routes/emails.py tests/test_emails_api.py
cd /Users/r/playground/hail
git add api/hailhq/api/routes/emails.py api/tests/test_emails_api.py
git commit -m "feat(api): emit the blended single-line email footer on the wire"
```

---

### Task 3: hail-website — signed internal org-name lookup endpoint

**Repo: `/Users/r/playground/hail-website` (separate git repo — commit there).**

**Files:**

- Create: `app/api/internal/organizations/lookup/route.ts`
- Test: `app/api/internal/organizations/lookup/__tests__/route.test.ts`

**Interfaces:**

- Consumes: `pool` from `@/lib/db` (pg Pool); `organizations` table (Better Auth: `id uuid`, `name text not null`); `HAIL_INTERNAL_SECRET` env var; the `X-Hail-Signature: sha256=<hex>` HMAC-over-raw-body scheme (mirror of `app/api/internal/usage-events/rate/route.ts`).
- Produces: `POST /api/internal/organizations/lookup` — request body `{"organization_id": "<uuid>"}`, responses: `200 {"name": "<org name>"}`, `404` unknown org, `400` bad body, `401` bad/missing signature, `503` secret unconfigured. Task 4's Python client consumes this contract.

- [ ] **Step 1: Write the failing test**

Create `app/api/internal/organizations/lookup/__tests__/route.test.ts`:

```typescript
import { createHmac } from "node:crypto";
import { beforeEach, describe, expect, it, vi } from "vitest";

const queryMock = vi.fn();
vi.mock("@/lib/db", () => ({
  pool: { query: (...args: unknown[]) => queryMock(...args) },
}));

import { POST } from "../route";

const SECRET = "test-secret";
const ORG_ID = "3f9c1a2e-8b4d-4c6e-9f0a-1b2c3d4e5f6a";

function signBody(body: string, secret = SECRET) {
  return `sha256=${createHmac("sha256", secret).update(body).digest("hex")}`;
}

function makeRequest(body: string, sig?: string) {
  return new Request("http://localhost/api/internal/organizations/lookup", {
    method: "POST",
    body,
    headers: {
      "Content-Type": "application/json",
      ...(sig ? { "X-Hail-Signature": sig } : {}),
    },
  });
}

describe("POST /api/internal/organizations/lookup", () => {
  beforeEach(() => {
    vi.stubEnv("HAIL_INTERNAL_SECRET", SECRET);
    queryMock.mockReset();
  });

  it("returns the org name for a valid signed request", async () => {
    queryMock.mockResolvedValue({ rows: [{ name: "Acme Corp" }] });
    const body = JSON.stringify({ organization_id: ORG_ID });
    const response = await POST(makeRequest(body, signBody(body)));
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ name: "Acme Corp" });
    expect(queryMock).toHaveBeenCalledWith(
      expect.stringContaining("FROM organizations"),
      [ORG_ID],
    );
  });

  it("rejects a missing signature with 401", async () => {
    const body = JSON.stringify({ organization_id: ORG_ID });
    const response = await POST(makeRequest(body));
    expect(response.status).toBe(401);
    expect(queryMock).not.toHaveBeenCalled();
  });

  it("rejects a signature minted with the wrong secret with 401", async () => {
    const body = JSON.stringify({ organization_id: ORG_ID });
    const response = await POST(
      makeRequest(body, signBody(body, "wrong-secret")),
    );
    expect(response.status).toBe(401);
    expect(queryMock).not.toHaveBeenCalled();
  });

  it("returns 404 for an unknown org id", async () => {
    queryMock.mockResolvedValue({ rows: [] });
    const body = JSON.stringify({ organization_id: ORG_ID });
    const response = await POST(makeRequest(body, signBody(body)));
    expect(response.status).toBe(404);
  });

  it("returns 404 (not 500) when pg rejects a non-uuid id", async () => {
    queryMock.mockRejectedValue(
      new Error("invalid input syntax for type uuid"),
    );
    const body = JSON.stringify({ organization_id: "not-a-uuid" });
    const response = await POST(makeRequest(body, signBody(body)));
    expect(response.status).toBe(404);
  });

  it("returns 400 for a body missing organization_id", async () => {
    const body = JSON.stringify({ nope: 1 });
    const response = await POST(makeRequest(body, signBody(body)));
    expect(response.status).toBe(400);
  });

  it("returns 503 when HAIL_INTERNAL_SECRET is unset", async () => {
    vi.stubEnv("HAIL_INTERNAL_SECRET", "");
    const body = JSON.stringify({ organization_id: ORG_ID });
    const response = await POST(makeRequest(body, signBody(body)));
    expect(response.status).toBe(503);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/r/playground/hail-website && npm test -- app/api/internal/organizations/lookup`
Expected: FAIL — cannot resolve `../route`.

- [ ] **Step 3: Write the route**

Create `app/api/internal/organizations/lookup/route.ts`:

```typescript
import { createHmac, timingSafeEqual } from "node:crypto";
import { NextResponse } from "next/server";
import { pool } from "@/lib/db";

export const dynamic = "force-dynamic";

/**
 * Internal endpoint hit by the hail API service when creating an outbound
 * call: resolves an organization's display name so the voicebot can speak
 * it in the TCPA identity disclosure. Verifies an HMAC-SHA256 of the raw
 * request body against `HAIL_INTERNAL_SECRET` (same scheme as
 * /api/internal/usage-events/rate).
 *
 * The caller treats every non-200 as "no name available" and falls back
 * to generic wording — so failure modes here only ever degrade copy,
 * never block a call.
 */
export async function POST(request: Request) {
  const secret = process.env.HAIL_INTERNAL_SECRET;
  if (!secret) {
    return NextResponse.json(
      { error: "HAIL_INTERNAL_SECRET not configured" },
      { status: 503 },
    );
  }

  const sigHeader = request.headers.get("x-hail-signature");
  if (!sigHeader || !sigHeader.startsWith("sha256=")) {
    return NextResponse.json(
      { error: "missing or malformed signature" },
      { status: 401 },
    );
  }

  const rawBody = await request.text();
  const expected = createHmac("sha256", secret).update(rawBody).digest("hex");
  const provided = sigHeader.slice("sha256=".length);

  // Constant-time compare — both are equal-length hex strings.
  let valid = false;
  try {
    valid =
      expected.length === provided.length &&
      timingSafeEqual(
        Buffer.from(expected, "hex"),
        Buffer.from(provided, "hex"),
      );
  } catch {
    valid = false;
  }
  if (!valid) {
    return NextResponse.json({ error: "invalid signature" }, { status: 401 });
  }

  let organizationId: unknown;
  try {
    ({ organization_id: organizationId } = JSON.parse(rawBody) ?? {});
  } catch {
    return NextResponse.json({ error: "invalid JSON body" }, { status: 400 });
  }
  if (typeof organizationId !== "string" || organizationId.length === 0) {
    return NextResponse.json(
      { error: "organization_id required" },
      { status: 400 },
    );
  }

  // pg raises on a malformed uuid literal — fold that into "not found"
  // rather than a 500, since the caller treats both identically.
  let rows: { name: string }[];
  try {
    ({ rows } = await pool.query<{ name: string }>(
      `SELECT name FROM organizations WHERE id = $1`,
      [organizationId],
    ));
  } catch {
    return NextResponse.json(
      { error: "organization not found" },
      { status: 404 },
    );
  }
  if (rows.length === 0) {
    return NextResponse.json(
      { error: "organization not found" },
      { status: 404 },
    );
  }
  return NextResponse.json({ name: rows[0].name });
}
```

- [ ] **Step 4: Run the tests**

Run: `cd /Users/r/playground/hail-website && npm test -- app/api/internal/organizations/lookup`
Expected: PASS, 7 tests.

- [ ] **Step 5: Run full website suite + build, then commit**

```bash
cd /Users/r/playground/hail-website
npm test
npx next build
git add app/api/internal/organizations/lookup
git commit -m "feat(internal): signed org-name lookup endpoint for the voice disclosure"
```

Expected: 293+ tests pass, clean build.

---

### Task 4: `fetch_organization_name` — fail-safe signed lookup (core)

**Files:**

- Modify: `core/hailhq/core/internal_webhook.py`
- Test: Create `core/tests/test_internal_webhook.py`

**Interfaces:**

- Consumes: Task 3's endpoint contract (`POST {HAIL_BASE_URL}/api/internal/organizations/lookup` → `200 {"name": ...}`); existing module plumbing (`_get_session()`, `settings.hail_base_url`, `settings.hail_internal_secret`, `hmac_signing.sign`); `hailhq.core.urls.join_url`.
- Produces: `async def fetch_organization_name(organization_id: str) -> str | None` — Task 5's route calls exactly this.

- [ ] **Step 1: Write the failing tests**

Create `core/tests/test_internal_webhook.py`:

```python
"""Tests for fetch_organization_name — every failure mode folds to None."""

from __future__ import annotations

import asyncio

import aiohttp
import pytest

from hailhq.core import internal_webhook
from hailhq.core.config import settings
from hailhq.core.internal_webhook import fetch_organization_name


class _FakeResponse:
    def __init__(self, status=200, payload=None, json_exc=None):
        self.status = status
        self._payload = payload
        self._json_exc = json_exc

    async def json(self):
        if self._json_exc is not None:
            raise self._json_exc
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.exc is not None:
            raise self.exc
        return self.response


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setattr(settings, "hail_base_url", "https://hail.so")
    monkeypatch.setattr(settings, "hail_internal_secret", "test-secret")


def _install(monkeypatch, session: _FakeSession) -> None:
    monkeypatch.setattr(internal_webhook, "_get_session", lambda: session)


async def test_returns_name_on_200(monkeypatch, configured):
    session = _FakeSession(response=_FakeResponse(200, {"name": "  Acme Corp  "}))
    _install(monkeypatch, session)

    assert await fetch_organization_name("org-123") == "Acme Corp"

    url, kwargs = session.calls[0]
    assert url == "https://hail.so/api/internal/organizations/lookup"
    assert kwargs["headers"]["X-Hail-Signature"].startswith("sha256=")


async def test_unset_config_returns_none_without_any_network_call(monkeypatch):
    monkeypatch.setattr(settings, "hail_base_url", "")
    monkeypatch.setattr(settings, "hail_internal_secret", "")

    def _boom():  # pragma: no cover — proves _get_session is never reached
        raise AssertionError("network layer must not be touched")

    monkeypatch.setattr(internal_webhook, "_get_session", _boom)
    assert await fetch_organization_name("org-123") is None


async def test_timeout_returns_none(monkeypatch, configured):
    _install(monkeypatch, _FakeSession(exc=asyncio.TimeoutError()))
    assert await fetch_organization_name("org-123") is None


async def test_non_200_returns_none(monkeypatch, configured):
    for status in (404, 500):
        _install(monkeypatch, _FakeSession(response=_FakeResponse(status)))
        assert await fetch_organization_name("org-123") is None


async def test_connection_error_returns_none(monkeypatch, configured):
    _install(monkeypatch, _FakeSession(exc=aiohttp.ClientConnectionError()))
    assert await fetch_organization_name("org-123") is None


async def test_malformed_body_returns_none(monkeypatch, configured):
    # json() raises (non-JSON body) …
    _install(
        monkeypatch,
        _FakeSession(response=_FakeResponse(200, json_exc=ValueError("not json"))),
    )
    assert await fetch_organization_name("org-123") is None
    # … or parses but has no usable name.
    for payload in ({"nope": 1}, {"name": ""}, {"name": "   "}, ["x"], None):
        _install(monkeypatch, _FakeSession(response=_FakeResponse(200, payload)))
        assert await fetch_organization_name("org-123") is None
```

Note: `core/tests` runs with `asyncio_mode = auto` (matching the existing suite) — no `@pytest.mark.asyncio` needed. If collection errors say otherwise, check `core/pyproject.toml` `[tool.pytest.ini_options]` and match whatever the existing async core tests do.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/r/playground/hail/core && uv run pytest -q tests/test_internal_webhook.py`
Expected: FAIL — `ImportError: cannot import name 'fetch_organization_name'`

- [ ] **Step 3: Implement**

In `core/hailhq/core/internal_webhook.py`:

Update the module docstring's first paragraph (it currently says the module is only fire-and-forget) by appending after the "HMAC:" line:

```python
"""...existing docstring...

Also hosts ``fetch_organization_name`` — the one request/response (not
fire-and-forget) call in this module: the API service resolves an org's
display name at call-creation time for the spoken TCPA disclosure, on a
tight budget, failing safe to ``None``.
"""
```

Add `from hailhq.core.urls import join_url` to the imports.

Add after `_TIMEOUT_SECONDS = 5`:

```python
# Org-name lookup budget: tight enough not to noticeably slow POST /calls,
# and every failure just degrades the spoken disclosure to generic wording.
_ORG_NAME_TIMEOUT_SECONDS = 1.0
```

Add this function after `_post` (before `notify_usage_event_recorded`):

```python
async def fetch_organization_name(organization_id: str) -> str | None:
    """Resolve an organization's display name from hail-website.

    Fail-safe by design: unset config (self-host — same posture as the
    fire-and-forget notifier), timeout, non-200, connection error,
    malformed body, or a blank name all return ``None``. Never raises —
    the call-creation path must not be able to fail on this.
    """
    if not settings.hail_base_url or not settings.hail_internal_secret:
        return None
    url = join_url(settings.hail_base_url, "api/internal/organizations/lookup")
    body = json.dumps(
        {"organization_id": organization_id}, separators=(",", ":")
    ).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Hail-Signature": sign(body, settings.hail_internal_secret),
    }
    try:
        async with _get_session().post(
            url,
            data=body,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=_ORG_NAME_TIMEOUT_SECONDS),
        ) as resp:
            if resp.status != 200:
                logger.warning(
                    "[internal_webhook] org-name lookup returned %s", resp.status
                )
                return None
            payload = await resp.json()
    except Exception:
        logger.warning("[internal_webhook] org-name lookup failed", exc_info=True)
        return None
    name = payload.get("name") if isinstance(payload, dict) else None
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/r/playground/hail/core && uv run pytest -q tests/test_internal_webhook.py`
Expected: PASS, 6 tests.

- [ ] **Step 5: Full core suite, lint, commit**

```bash
cd /Users/r/playground/hail/core && uv run pytest -q && uv run ruff check hailhq/core/internal_webhook.py tests/test_internal_webhook.py
cd /Users/r/playground/hail
git add core/hailhq/core/internal_webhook.py core/tests/test_internal_webhook.py
git commit -m "feat(core): fail-safe org-name lookup against hail-website"
```

---

### Task 5: `POST /calls` — resolve the name and ship it in dispatch metadata (api)

**Files:**

- Modify: `api/hailhq/api/routes/calls.py` (import + lookup call + metadata key)
- Modify: `api/hailhq/api/main.py` (lifespan closes the shared aiohttp session)
- Test: `api/tests/test_calls_api.py` (two new tests)

**Interfaces:**

- Consumes: `fetch_organization_name(organization_id: str) -> str | None` from Task 4.
- Produces: dispatch metadata key `"org_name": str | None` alongside the existing `"first_message"` — Task 6's voicebot reads exactly `metadata.get("org_name")`.

- [ ] **Step 1: Write the failing tests**

Append to `api/tests/test_calls_api.py` (near the other `test_post_calls_*` tests; it uses the same `client`/`org_and_key`/`livekit_mock`/`add_phone_number` fixtures already imported by the module):

```python
async def test_post_calls_dispatch_metadata_carries_org_name(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[str, ApiKey, str],
    livekit_mock: AsyncMock,
    add_phone_number,
    monkeypatch,
) -> None:
    """The resolved org name rides the (server-built) dispatch metadata."""
    org_id, _, plain = org_and_key
    await add_phone_number(async_session, org_id)

    async def fake_lookup(organization_id: str) -> str | None:
        assert organization_id == str(org_id)
        return "Acme Corp"

    monkeypatch.setattr(
        "hailhq.api.routes.calls.fetch_organization_name", fake_lookup
    )

    resp = await client.post(
        "/calls",
        json={"to": "+14155559999", "system_prompt": "hi", "recipient_consent": True},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 201
    dispatch_kwargs = livekit_mock.dispatch_agent.await_args.kwargs
    assert dispatch_kwargs["metadata"]["org_name"] == "Acme Corp"


async def test_post_calls_dispatch_metadata_org_name_none_on_lookup_failure(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[str, ApiKey, str],
    livekit_mock: AsyncMock,
    add_phone_number,
    monkeypatch,
) -> None:
    """Lookup failure degrades to org_name=None — the call still goes out."""
    org_id, _, plain = org_and_key
    await add_phone_number(async_session, org_id)

    async def fake_lookup(organization_id: str) -> str | None:
        return None

    monkeypatch.setattr(
        "hailhq.api.routes.calls.fetch_organization_name", fake_lookup
    )

    resp = await client.post(
        "/calls",
        json={"to": "+14155559999", "system_prompt": "hi", "recipient_consent": True},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 201
    dispatch_kwargs = livekit_mock.dispatch_agent.await_args.kwargs
    assert dispatch_kwargs["metadata"]["org_name"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/r/playground/hail/api && uv run pytest -q tests/test_calls_api.py -k org_name`
Expected: FAIL — `AttributeError: ... has no attribute 'fetch_organization_name'` (monkeypatch target doesn't exist yet).

- [ ] **Step 3: Wire the route**

In `api/hailhq/api/routes/calls.py`:

Add to the imports (next to the other `hailhq.core` imports):

```python
from hailhq.core.internal_webhook import fetch_organization_name
```

Immediately before the external-calls section (the lines reading `# 4. External calls — best-effort with status reconciliation.` / `room_name: str | None = None`, ~line 300), add:

```python
    # Resolve the org's display name for the spoken TCPA identity
    # disclosure (47 CFR 64.1200(b)(1)). Fail-safe: any lookup failure →
    # None → the voicebot speaks the generic fallback line instead.
    org_name = await fetch_organization_name(str(call.organization_id))
```

In the `lk.dispatch_agent(...)` metadata dict (~line 328), add one key after `"first_message": body.first_message,`:

```python
                "org_name": org_name,
```

(Note: this dict is server-built — deliberately distinct from the caller-supplied `body.metadata` that goes into `call_metadata` at ~line 258. Keep it that way; `org_name` must not be caller-injectable.)

- [ ] **Step 4: Close the shared session in the api lifespan**

In `api/hailhq/api/main.py`: add `from hailhq.core import internal_webhook` to the imports, and in the lifespan `finally:` block (~line 188), add before `await calls_routes.close_livekit_singleton()`:

```python
        await internal_webhook.aclose()
```

(Until now only the voicebot used this module's aiohttp session; the api process now opens it too and must close it on shutdown.)

- [ ] **Step 5: Run the full api suite**

Run: `cd /Users/r/playground/hail/api && uv run pytest -q`
Expected: PASS. Existing calls tests are unaffected: in the test environment `hail_base_url` is unset, so the un-monkeypatched `fetch_organization_name` returns `None` immediately with no network attempt.

- [ ] **Step 6: Regenerate nothing — confirm no OpenAPI change, lint, commit**

`POST /calls`'s request/response schemas are untouched (dispatch metadata is internal), so `openapi/openapi.yaml` must NOT change. Verify:

```bash
cd /Users/r/playground/hail
git status --porcelain openapi/
```

Expected: no output.

```bash
cd /Users/r/playground/hail/api && uv run ruff check hailhq/api/routes/calls.py hailhq/api/main.py tests/test_calls_api.py
cd /Users/r/playground/hail
git add api/hailhq/api/routes/calls.py api/hailhq/api/main.py api/tests/test_calls_api.py
git commit -m "feat(api): resolve org name at call creation for the spoken disclosure"
```

---

### Task 6: Voicebot — speak the name, tighten the fallback (voicebot)

**Files:**

- Modify: `voicebot/hailhq/voicebot/agent.py:109-140` (disclosure block + `speak_greeting`), `~247` (`parse_metadata` docstring), `~753` (`__all__`)
- Test: `voicebot/tests/test_agent.py` (update one assertion comment block, add two tests)

**Interfaces:**

- Consumes: dispatch metadata key `"org_name": str | None` from Task 5 (already parsed — `parse_metadata` passes all keys through).
- Produces: `disclosure_line(org_name: str | None) -> str`; `AI_DISCLOSURE_LINE` remains exported as the generic fallback text (existing tests import it).

- [ ] **Step 1: Write the failing tests**

In `voicebot/tests/test_agent.py`, after `test_speak_greeting_first_message_cannot_precede_disclosure` (~line 976), add:

```python
async def test_speak_greeting_names_the_org_when_dispatch_metadata_has_it() -> None:
    """org_name (server-resolved, not caller-supplied) is spoken in the
    disclosure — 47 CFR 64.1200(b)(1) wants the initiating business named."""
    session = FakeAnnouncingSession()

    await speak_greeting(session, {"org_name": "Acme Corp"})

    assert session.say_calls == [
        ("Hi, this is an AI assistant calling on behalf of Acme Corp.", True)
    ]


async def test_speak_greeting_blank_or_missing_org_name_falls_back_to_generic() -> None:
    for meta in ({}, {"org_name": None}, {"org_name": ""}, {"org_name": "   "}):
        session = FakeAnnouncingSession()
        await speak_greeting(session, meta)
        assert session.say_calls == [(AI_DISCLOSURE_LINE, True)]
```

- [ ] **Step 2: Run tests to verify the new ones fail and pin the old text**

Run: `cd /Users/r/playground/hail/voicebot && uv run pytest -q tests/test_agent.py -k speak_greeting`
Expected: the two new tests FAIL (`org_name` ignored today → generic line spoken / old 17-word text); the three existing ones PASS.

- [ ] **Step 3: Implement**

In `voicebot/hailhq/voicebot/agent.py`, replace the block from the `# Proactive AI disclosure` comment (~line 109) through the end of `speak_greeting` (~line 140) with:

```python
# Proactive AI disclosure — spoken unconditionally as the first thing on
# every call, immediately after session.start(). Unlike VOICE_PREAMBLE (LLM
# instructions the model could ignore), this is a literal session.say() so
# it is a real, enforced disclosure, not a prompt hope. When the API
# resolved the requesting organization's display name, the line names it —
# 47 CFR 64.1200(b)(1) requires identifying the initiating business at the
# start of an artificial-voice call — otherwise it falls back to generic
# wording. Only the name is interpolated; the template is hardcoded and
# not reachable/overridable via the public API: org_name arrives in the
# server-built dispatch metadata (resolved from the org record), never
# from body.system_prompt, body.first_message, or body.metadata.
AI_DISCLOSURE_LINE = (
    "Hi, this is an AI assistant calling on behalf of whoever requested "
    "this call."
)


def disclosure_line(org_name: str | None) -> str:
    """The exact disclosure to speak — named when the org name resolved."""
    if org_name and org_name.strip():
        return (
            "Hi, this is an AI assistant calling on behalf of "
            f"{org_name.strip()}."
        )
    return AI_DISCLOSURE_LINE


async def speak_greeting(session: AgentSession, metadata: dict[str, Any]) -> None:
    """Speak the mandatory AI disclosure, then the caller's ``first_message`` if set.

    The disclosure is unconditional and always first. Its template is not
    reachable via caller-controlled fields (``body.system_prompt`` /
    ``body.first_message``); only ``org_name`` — resolved server-side by
    the API from the organization record — is interpolated into it. Call
    this right after ``session.start()``.
    """
    await session.say(
        disclosure_line(metadata.get("org_name")), allow_interruptions=True
    )
    if metadata.get("first_message"):
        await session.say(metadata["first_message"], allow_interruptions=True)
```

In `parse_metadata`'s docstring (~line 247), extend the optional-keys sentence to:

```python
    Required: ``call_id`` (returned as a parsed :class:`UUID`). Optional:
    ``voice_config``, ``system_prompt``, ``llm`` (None → mode A fallback
    chain), ``first_message``, ``org_name`` (server-resolved display name
    spoken in the AI disclosure; absent/None → generic wording).
```

In `__all__` (~line 753), add `"disclosure_line",` next to `"AI_DISCLOSURE_LINE",`.

- [ ] **Step 4: Run the voicebot suite**

Run: `cd /Users/r/playground/hail/voicebot && uv run pytest -q`
Expected: PASS (57 + 2 new). The three pre-existing greeting tests pass unchanged because they assert against the `AI_DISCLOSURE_LINE` constant, whose _text_ changed but whose role (what's spoken with no org_name) did not.

- [ ] **Step 5: Lint and commit**

```bash
cd /Users/r/playground/hail/voicebot && uv run ruff check hailhq/voicebot/agent.py tests/test_agent.py
cd /Users/r/playground/hail
git add voicebot/hailhq/voicebot/agent.py voicebot/tests/test_agent.py
git commit -m "feat(voicebot): speak the org name in the AI disclosure, tighten fallback"
```

---

### Task 7: Cross-cutting verification + doc touch-ups

**Files:**

- Modify: `hail-website/content/legal/facts.md` (maintenance-checklist source of truth — one line)
- No other code changes: this task is verification.

- [ ] **Step 1: Full suites, both repos**

```bash
cd /Users/r/playground/hail/core && uv run pytest -q
cd /Users/r/playground/hail/api && uv run pytest -q
cd /Users/r/playground/hail/voicebot && uv run pytest -q
cd /Users/r/playground/hail/mcp && uv run pytest -q
cd /Users/r/playground/hail-website && npm test
```

Expected: all green (core 348+6, api 276+2, voicebot 57+2, mcp 61, website 293+7 — counts approximate; zero failures is the requirement).

- [ ] **Step 2: Record the data-flow change in the legal facts sheet**

Per `hail-website/content/legal/facts.md`'s own maintenance checklist ("Architecture/feature change that touches data handling"), append one bullet to the `## Product / architecture` section:

```markdown
- Voice disclosure identity: at call creation the hail API resolves the
  organization's display name from hail-website (signed internal lookup,
  1s timeout, fail-safe to generic wording) and the voicebot speaks it in
  the opening AI disclosure — per 47 CFR 64.1200(b)(1)'s requirement to
  identify the initiating business. Recipients therefore hear the
  Developer org's name; no new data category is stored.
```

Commit in the website repo:

```bash
cd /Users/r/playground/hail-website
git add content/legal/facts.md
git commit -m "docs(legal): record voice-disclosure org-name flow in facts sheet"
```

- [ ] **Step 3: Manual smoke check (optional, needs configured env)**

With both services running locally and `HAIL_BASE_URL`/`HAIL_INTERNAL_SECRET` set on the API:

1. `POST /calls` for an org whose website record has a name → dispatch metadata (visible in API logs / LiveKit dashboard) carries `"org_name": "<name>"`.
2. Stop hail-website, `POST /calls` again → call still returns 201, metadata carries `"org_name": null`, API log shows one `org-name lookup failed` warning.

---

## Self-Review (completed at plan time)

- **Spec coverage:** decision 1 → Tasks 1-2; decision 2 → Task 6; decisions 3-6 (lookup, placement, signing, timeout/fail-safe/self-host) → Tasks 3-5; spec's testing section → each task's test steps (six `fetch_organization_name` failure modes in Task 4, signature/404 cases in Task 3, metadata cases in Task 5, greeting cases in Task 6); facts.md maintenance rule → Task 7.
- **Placeholder scan:** none — every step carries full code.
- **Type consistency:** `fetch_organization_name(organization_id: str) -> str | None` (Task 4) matches Task 5's call `await fetch_organization_name(str(call.organization_id))` and monkeypatch target `hailhq.api.routes.calls.fetch_organization_name`; metadata key `"org_name"` matches Task 6's `metadata.get("org_name")`; endpoint path `api/internal/organizations/lookup` identical in Tasks 3 and 4; `SENT_FOOTER_TEXT`/`append_sent_footer` identical in Tasks 1 and 2.
