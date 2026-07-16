# SMS Billing — Plan 3: Number Release Path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give an org a way to release a dedicated number so its at-cost monthly fee stops — closing the "billed forever, no exit" gap. Without this, Plan 2's `released_at IS NULL` predicate is unreachable dead code.

**Architecture:** A new `POST /numbers/{id}/release` endpoint in hail (FastAPI) releases the number at the carrier via the existing `VoiceProvider.release_number`, then sets `released_at = now()` and `provisioning_state = 'released'`. OpenAPI is regenerated in the same PR. In hail-website, `getNumbers` surfaces `provisioning_state`, a `releaseNumberAction` server action calls the endpoint, and `NumbersPanel` gains a confirm-gated Release control mirroring the existing destructive-action pattern.

**Tech Stack:** FastAPI async handlers + SQLAlchemy async, pytest, Next.js server actions + React client component, vitest (hail-website has no test for the console UI — follows existing untested panel pattern).

## Global Constraints

- Two repos, branch `feat/sms-console-ui` in each:
  - hail: `/Users/r/playground/hail/.claude/worktrees/sms-console-ui`
  - hail-website: `/Users/r/playground/hail-website/.claude/worktrees/sms-console-ui`
- **Depends on Plan 2** — the rater's `released_at IS NULL` clause is what makes release stop billing.
- hail invariant (repo CLAUDE.md): **regenerate `openapi/openapi.yaml` in the same PR as any route change.** `openapi-check.yml` CI fails otherwise.
- Release uses the **voice** provider (`get_voice_provider`) — `release_number` lives on `VoiceProvider`, not `SmsProvider`.
- Release is destructive/irreversible → the console control is gated behind `confirm()`, mirroring `SuppressionPanel.tsx`.
- Deploy order: hail first (endpoint), then hail-website (UI).
- Commit after each task. Conventional Commits. No `Co-Authored-By` trailer.

---

### Task 1: `POST /numbers/{id}/release` endpoint (hail)

**Files:**
- Modify: `api/hailhq/api/routes/numbers.py`
- Test: `api/tests/test_numbers_api.py`

**Interfaces:**
- Consumes: `get_voice_provider` (existing, `numbers.py:49-58`), `_get_org_number_or_404` (existing, `numbers.py:61-77`), `VoiceProvider.release_number(provider_resource_id)` (existing).
- Produces: `POST /numbers/{id}/release` → releases at carrier, sets `released_at`/`provisioning_state='released'`, returns `PhoneNumberResponse`. Idempotent on an already-released number; 404 for another org's number.

- [ ] **Step 1: Write the failing test**

In `api/tests/test_numbers_api.py`, mirror the enable-sms happy-path test (fixtures `client`, `async_session`, `org_and_key`, `voice_provider_mock`; seed a `PhoneNumber` inline with `provider_resource_id="PN_release_me"`, `provisioning_state="active"`, `is_pool=False`, `capabilities=["voice","sms"]`). Add:

```python
async def test_release_number_releases_at_carrier_and_marks_released(
    client, async_session, org_and_key, voice_provider_mock
) -> None:
    org_id, _, plaintext = org_and_key
    pn = PhoneNumber(
        organization_id=org_id,
        e164="+13105550000",
        country_code="US",
        number_type="local",
        capabilities=["voice", "sms"],
        provider_resource_id="PN_release_me",
        provisioning_state="active",
        is_pool=False,
    )
    async_session.add(pn)
    await async_session.commit()

    resp = await client.post(
        f"/numbers/{pn.id}/release",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["provisioning_state"] == "released"
    voice_provider_mock.release_number.assert_awaited_once_with("PN_release_me")

    await async_session.refresh(pn)
    assert pn.provisioning_state == "released"
    assert pn.released_at is not None


async def test_release_number_404_for_another_org(
    client, async_session, org_and_key, voice_provider_mock
) -> None:
    import uuid
    org_id, _, plaintext = org_and_key
    resp = await client.post(
        f"/numbers/{uuid.uuid4()}/release",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 404, resp.text
    voice_provider_mock.release_number.assert_not_awaited()


async def test_release_number_is_idempotent(
    client, async_session, org_and_key, voice_provider_mock
) -> None:
    org_id, _, plaintext = org_and_key
    pn = PhoneNumber(
        organization_id=org_id,
        e164="+13105550001",
        country_code="US",
        number_type="local",
        capabilities=["voice", "sms"],
        provider_resource_id="PN_already",
        provisioning_state="released",
        is_pool=False,
    )
    async_session.add(pn)
    await async_session.commit()

    resp = await client.post(
        f"/numbers/{pn.id}/release",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 200, resp.text
    # Already released: don't call the carrier again.
    voice_provider_mock.release_number.assert_not_awaited()
```

(Ensure `PhoneNumber` is imported in the test module — the enable-sms tests already import it; reuse that import.)

- [ ] **Step 2: Run to verify fail**

Run: `cd api && uv run pytest tests/test_numbers_api.py -k release -v`
Expected: FAIL — 404/405 (route does not exist).

- [ ] **Step 3: Implement the endpoint**

In `api/hailhq/api/routes/numbers.py`, add after `enable_sms` (mirror its signature order; inject the **voice** provider). `datetime` import: use `from datetime import datetime, timezone` if not already imported (check the top of the file — `acquired_at`/`released_at` are `TS`; use `datetime.now(timezone.utc)`).

```python
@router.post("/{number_id}/release", response_model=PhoneNumberResponse)
async def release_number(
    number_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    provider: Annotated[VoiceProvider, Depends(get_voice_provider)],
) -> PhoneNumberResponse:
    number = await _get_org_number_or_404(db, number_id, principal.organization_id)

    # Idempotent: an already-released number stays released; don't re-hit the
    # carrier (the resource is already gone — a second delete would error).
    if number.provisioning_state == "released":
        return PhoneNumberResponse.model_validate(number)

    await provider.release_number(number.provider_resource_id)

    number.provisioning_state = "released"
    number.released_at = datetime.now(timezone.utc)
    await db.commit()
    return PhoneNumberResponse.model_validate(number)
```

Confirm `VoiceProvider` is imported (it is used via `get_voice_provider`; the type import is `from hailhq.core.providers.voice import VoiceProvider` — check the file head and add if missing).

- [ ] **Step 4: Run tests to verify pass**

Run: `cd api && uv run pytest tests/test_numbers_api.py -k release -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Run the full numbers suite + lint**

Run: `cd api && uv run pytest tests/test_numbers_api.py -q && cd .. && uvx ruff check api/hailhq/api/routes/numbers.py && uvx black --check api/hailhq/api/routes/numbers.py api/tests/test_numbers_api.py`
Expected: all pass, black clean. (Run `uvx black` on the two files if `--check` fails.)

- [ ] **Step 6: Commit**

```bash
git add api/hailhq/api/routes/numbers.py api/tests/test_numbers_api.py
git commit -m "feat(api): POST /numbers/{id}/release to release a dedicated number"
```

---

### Task 2: Regenerate OpenAPI (hail)

**Files:**
- Modify: `openapi/openapi.yaml`

**Interfaces:**
- Produces: `openapi.yaml` includes the `/numbers/{number_id}/release` path so `openapi-check.yml` passes and the Go CLI codegen sees it.

- [ ] **Step 1: Start the API locally**

Run (in one shell): `cd api && uv run uvicorn hailhq.api.main:app --port 8080`
Expected: server starts on :8080.

- [ ] **Step 2: Regenerate the spec**

Run (in another shell, from repo root), the command documented in `docs/contributing.md`:
```bash
curl -s http://localhost:8080/openapi.json \
  | python -c "import json, sys, yaml; yaml.safe_dump(json.load(sys.stdin), sys.stdout, sort_keys=False)" \
  > openapi/openapi.yaml
```
Expected: `openapi/openapi.yaml` now contains a `/numbers/{number_id}/release` entry. Stop the server.

- [ ] **Step 3: Verify the diff is only the new path**

Run: `git diff --stat openapi/openapi.yaml` and skim `git diff openapi/openapi.yaml`.
Expected: additions are the release path + operation; no unrelated churn. (If ordering churn appears, that's acceptable — the CI check is semantic.)

- [ ] **Step 4: Run the openapi check the way CI does**

Run: `uv sync --all-packages --all-extras` then the in-process compare from `.github/workflows/openapi-check.yml` (regenerate app.openapi() and diff against the committed file).
Expected: no semantic difference.

- [ ] **Step 5: Commit**

```bash
git add openapi/openapi.yaml
git commit -m "docs(openapi): regenerate for POST /numbers/{id}/release"
```

---

### Task 3: Surface `provisioning_state` in `getNumbers` (hail-website)

**Files:**
- Modify: `lib/sms-queries.ts`
- Test: `lib/__tests__/` (add if a sms-queries test exists; otherwise this is exercised via the panel — see note)

**Interfaces:**
- Consumes: the `phone_numbers.provisioning_state` column.
- Produces: `PhoneNumberRow` gains `provisioningState: string`; `getNumbers` selects it. Lets the panel hide/label released numbers and gate the Release control.

- [ ] **Step 1: Add the column to the query and type**

In `lib/sms-queries.ts`, `getNumbers(orgId)` selects `id, e164, country_code, capabilities, messaging_service_sid, created_at`. Add `provisioning_state` to the SELECT and `released_at`:
```sql
SELECT id, e164, country_code, capabilities, messaging_service_sid,
       provisioning_state, created_at
  FROM phone_numbers
 WHERE organization_id = $1 AND is_pool = FALSE
 ORDER BY created_at ASC
```
Add to the `PhoneNumberRow` type: `provisioningState: string;` and map `provisioning_state` → `provisioningState` in the row mapper.

- [ ] **Step 2: Build to typecheck**

Run: `npm run build`
Expected: build succeeds. (No unit test exists for `sms-queries` — it queries `pool` directly; the mapping is exercised by the panel render. If a `sms-queries.test.ts` exists, add a mapping assertion there.)

- [ ] **Step 3: Commit**

```bash
git add lib/sms-queries.ts
git commit -m "feat(console): expose provisioning_state on number rows"
```

---

### Task 4: `releaseNumberAction` server action (hail-website)

**Files:**
- Modify: `app/console/sms/actions.ts`

**Interfaces:**
- Consumes: `orgApiAction` (existing helper, `actions.ts:29-39`) — admin-gates, calls the hail API as the org, folds the response.
- Produces: `releaseNumberAction(numberId: string)` → `POST /numbers/{id}/release`, mirroring `enableSmsOnNumberAction`.

- [ ] **Step 1: Add the action**

In `app/console/sms/actions.ts`, after `enableSmsOnNumberAction`, add (same shape — `orgApiAction`, POST, encode the id):

```typescript
export async function releaseNumberAction(numberId: string) {
  return orgApiAction<{ id: string; provisioning_state: string }>(
    `/numbers/${encodeURIComponent(numberId)}/release`,
    { method: "POST" },
  );
}
```

- [ ] **Step 2: Build**

Run: `npm run build`
Expected: build succeeds.

- [ ] **Step 3: Commit**

```bash
git add app/console/sms/actions.ts
git commit -m "feat(console): releaseNumberAction server action"
```

---

### Task 5: Release control in `NumbersPanel` (hail-website)

**Files:**
- Modify: `app/console/sms/NumbersPanel.tsx`

**Interfaces:**
- Consumes: `releaseNumberAction` (Task 4), `provisioningState` on `PhoneNumberRow` (Task 3).
- Produces: a confirm-gated "Release" button per active dedicated number; released numbers render as released and show no Release button.

- [ ] **Step 1: Add release state + handler**

In `NumbersPanel.tsx`, mirror the Enable-SMS transition pattern and the `SuppressionPanel` confirm pattern. Add near the other `useTransition`/`useState` hooks:
```tsx
const [releasing, startRelease] = useTransition();
const [releasingId, setReleasingId] = useState<string | null>(null);
```
Import the action:
```tsx
import { acquireNumberAction, enableSmsOnNumberAction, releaseNumberAction } from "./actions";
```
Add the handler (confirm-gated, like `SuppressionPanel.onRemove`):
```tsx
function onRelease(numberId: string, e164: string) {
  if (
    !confirm(
      `Release ${e164}? This gives the number up at the carrier and stops its ` +
        `monthly fee. You cannot get the same number back. This can't be undone.`,
    )
  ) {
    return;
  }
  setError(null);
  setReleasingId(numberId);
  startRelease(async () => {
    const result = await releaseNumberAction(numberId);
    if (!result.ok) setError(result.error);
    else router.refresh();
    setReleasingId(null);
  });
}
```

- [ ] **Step 2: Render the button**

In the row actions (`<div className="c-row-actions">`), add a Release button for active dedicated numbers (not already released), styled destructive like `SuppressionPanel`'s (`className="danger"`). Gate on `canManage` and `n.provisioningState !== "released"`:
```tsx
{canManage && n.provisioningState !== "released" && (
  <button
    className="danger"
    disabled={releasing && releasingId === n.id}
    onClick={() => onRelease(n.id, n.e164)}
  >
    {releasing && releasingId === n.id ? "Releasing…" : "Release"}
  </button>
)}
```
Optionally, for a released row, render a muted "Released" label instead of the action buttons (mirror how the panel shows non-actionable state elsewhere).

- [ ] **Step 3: Build**

Run: `npm run build`
Expected: build succeeds. (No test harness for the panel — this follows the existing untested `NumbersPanel`/`SuppressionPanel` pattern. Verify by eye in Step 4.)

- [ ] **Step 4: Manual verification (record for the operator)**

After deploy: on `/console/sms`, an active dedicated number shows a "Release" button; clicking it prompts a confirm; confirming releases at the carrier and the row re-renders as released. Confirm the number's monthly fee stops on the next rater run (Plan 2). Document in the PR.

- [ ] **Step 5: Commit**

```bash
git add app/console/sms/NumbersPanel.tsx
git commit -m "feat(console): confirm-gated Release control for dedicated numbers"
```

---

## Self-Review

**Spec coverage** (against the design spec's "Release path" section and Decision 5):
- `POST /numbers/{id}/release` → Task 1 (via voice provider; idempotent; 404 cross-org — all three tested). ✓
- `released_at` + `provisioning_state='released'` writes → Task 1. ✓
- OpenAPI regen in same PR → Task 2 (documented command + CI-parity check). ✓
- Console Release control, confirm-gated → Tasks 4–5. ✓
- `getNumbers` surfaces released state → Task 3. ✓
- Rater `released_at IS NULL` stops the fee → satisfied by Plan 2's predicate; Task 5 Step 4 verifies the end-to-end effect. ✓

**Placeholder scan:** none — every code step shows complete code; the one "optional" muted-Released label (Task 5 Step 2) is explicitly optional polish, not a required deliverable with hidden detail.

**Type consistency:** `provisioningState` (camelCase) defined on `PhoneNumberRow` in Task 3 and read in Task 5. `releaseNumberAction(numberId: string)` defined in Task 4, called in Task 5. The endpoint returns `PhoneNumberResponse` (Task 1) whose `provisioning_state` the action types as `{ id, provisioning_state }` (Task 4) — snake_case from the API, matching the existing `enableSmsOnNumberAction` return shape.

**Cross-plan note:** This plan's value depends on Plan 2's rater predicate (`released_at IS NULL`). Ship Plan 2 first (or together) so releasing actually stops billing rather than leaving the clause dormant.
