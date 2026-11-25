# Connected numbers, plan 2: public API, CLI, MCP, console, pricing line

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Customers connect their own phone system (PBX) to Hail by themselves: through the API, the CLI and the console. A number someone claims but never checks can not block its real owner.

**Architecture:** Plan 1 (backend) shipped the service layer in `api/hailhq/api/connected_numbers.py` and an ops command. This plan adds:

- the "unchecked claims give way" rule in that service;
- thin FastAPI routes over it, the OpenAPI file and the Go CLI generated from it;
- the new number fields in the MCP and SDK models;
- the extension field on contacts;
- public docs.

hail-website then gets the console panel and, last, one pricing line.

**Tech Stack:** FastAPI, SQLAlchemy async, Pydantic v2, pytest; Go (cobra, oapi-codegen); Next.js console (server actions, vitest, Playwright).

**Spec:** `docs/superpowers/specs/2026-11-18-pbx-connected-numbers-design.md` (sections 5, 6, 7, 8, "Order of work", "Plan 2 must"). Plan 1: `docs/superpowers/plans/2026-11-18-pbx-connected-numbers-backend.md`, section "Interfaces for plan 2".

## Global Constraints

- Owner decisions:
  - Check line: the ownership-check line stays English, no change (2026-11-25).
  - SDK: only the new number fields, no `sip_connections` resource (2026-11-25).
- Unchecked claims give way (owner, 2026-11-24). A Hail purchase or another connection wants a number held only by an `unverified` claim: Hail takes the number off that claim's trunk, releases the claim, then goes ahead. The cap of 10 unverified numbers per workspace stays (`UNVERIFIED_PER_ORG`).
- Builder rule, claim with a running check: a claim whose ownership check is running (`check_in_flight` is not None, at most `CHECK_WINDOW` = 107 s) does not give way. The other side gets 409 with code `check_in_progress` ("This number is being checked. Try again in 2 minutes."). This stops two workspaces displacing each other while one of them checks.
- MCP: no tool creates a connection or runs the check. `list_numbers`, `get_number`, `route_number` show `source`.
- Prices (unchanged, from `PROVIDER_POLICIES.pbx` in hail-website `lib/private-rates.ts`):
  - call 10¢ a minute;
  - handover leg +10¢ a minute;
  - recording +1¢ a minute;
  - no monthly fee.
- Pricing page line (spec, exact copy): "Connect your phone system: 10¢ a minute, no monthly fee". Read the numbers from `PROVIDER_POLICIES.pbx`, never type them.
- Passwords show once: in the create and rotate responses only. Never store them in `idempotency_keys`, an audit payload or a log. List and get show `password_last4` only.
- Public repo: no real hostnames, trunk ids or phone numbers in code, tests, docs or PR bodies. Use `+14155550100` and `pbx.example.com`.
- New env key `LIVEKIT_SIP_HOST` (per-environment: the LiveKit project's SIP host, `<project>.sip.livekit.cloud`). Empty: the connection routes answer 503 `not_configured`.
  - Add it to `.env.example` under "LiveKit Cloud".
  - Write its real value into the main checkout `.env`. Get the value from the LiveKit host in the Twilio trunk's origination URL.
  - Run the parity diff.
- Every new route regenerates `openapi/openapi.yaml` and `cli/internal/client/client.gen.go` in the same PR.
- Marketing and console copy names no vendor (no "3CX", no "LiveKit"). The docs setup page may name vendors.

## Review Focus

1. **Same-workspace give-way.** A workspace re-adds or buys a number that its own unverified claim holds. Expected: no deadlock, and the claim gives way. Test: `test_add_number_takes_over_own_unverified_claim`, `test_purchase_takes_over_own_unverified_claim`.
2. **Give-way while that claim's check is running.** Expected: 409 `check_in_progress`, claim untouched, trunk untouched. Test: `test_claim_with_running_check_does_not_give_way` (service) and the purchase route test.
3. **LiveKit fails while removing the claim.** Expected: the claim stays as it was, and the purchase or add answers 502 `livekit_failed`. No money is held and no row is inserted. Test: `test_give_way_livekit_failure_keeps_claim`.
4. **Password leaks.** Create with an `Idempotency-Key` header, then list and get. Expected: no idempotency row; list and get carry no password field. Test: `test_create_connection_password_only_in_create_response`.
5. **Verify request length.** The check can take up to `CHECK_WINDOW` (107 s). Expected:
   - the console action allows 120 s (`maxDuration`) and aborts its fetch at 115 s;
   - the CLI sets no shorter timeout.
     Test: the console `verifyConnectedNumber` test checks the signal; a CLI test checks `verify` sends no timeout under 120 s.

---

# Part A: hail repo, one PR

Branch `feat/pbx-connected-numbers-api`. Worktree `.claude/worktrees/feat-pbx-connected-numbers-api`. Test DB: a compose project named `hail-pbx-connected-numbers-api` (container `hail-pbx-connected-numbers-api-postgres-1`).

### Task A1: Unchecked claims give way

**Files:**

- Modify: `api/hailhq/api/connected_numbers.py` (new function; `add_connected_number` calls it before its org lock)
- Modify: `api/hailhq/api/number_orders.py` (`acquire_offer` calls it before its first `org_lock`)
- Modify: `api/hailhq/api/routes/numbers.py` (acquire route gets `lk: LiveKitClient | None = Depends(get_livekit_optional)` and passes it through; maps `ConnectedNumberError` codes from the give-way)
- Modify: `api/hailhq/api/connected_numbers_ops.py` (new subcommand `release-claim <e164>`, for the by-hand DIDWW insert)
- Modify: `docs/operations/didww-by-request.md` (one step before the INSERT: run `release-claim`), `docs/operations/connected-numbers.md` (the rule, one short section)
- Test: `api/tests/test_connected_numbers.py`, `api/tests/test_number_orders.py` (or the existing acquire route test file)

**Interfaces:**

- Consumes: `remove_connected_number(db, lk, number, only_state="unverified")`, `check_in_flight(db, number_id, now)` (`core/hailhq/core/sip_connections.py`), `org_lock`, `write_audit_log` (`api/hailhq/api/audit.py`).
- Produces:

```python
async def give_way_unchecked_claim(
    db: AsyncSession, lk: LiveKitClient | None, e164: str, *, now: datetime | None = None
) -> bool
# Ends the caller's transaction (commit or rollback). Call it before the
# caller takes any lock. Returns True when a claim was released.
# - no live row for e164, or the live row is not provider 'pbx' in state
#   'unverified': rollback, return False (the caller's unique checks decide).
# - otherwise: org_lock(db, claim.organization_id), refresh, re-check state
#   and check_in_flight under that lock.
#   - check running: rollback, raise ConnectedNumberError("check_in_progress", "e164",
#     "This number is being checked. Try again in 2 minutes.")
#   - lk is None: rollback, raise ConnectedNumberError("not_configured")
#   - else remove_connected_number(db, lk, claim, only_state="unverified")
#     (same session: the advisory lock is re-entrant), then audit
#     "number.claim_released" on the CLAIM's organization, payload
#     {"e164": e164}; return True.
```

Callers:

- `add_connected_number`: right after the e164 is normalized, before `org_lock`.
- `acquire_offer`: read the quote's e164 with `load_quote` and no lock, call `give_way_unchecked_claim`, then continue as today. Today's live-row check and the `IntegrityError` 409 stay as the final judge. The org lock is taken again after the call.
- Route error mapping for the purchase:
  - `check_in_progress`: 409, its message.
  - `livekit_failed`: 502.
  - `not_configured`: keep today's 409 `NUMBER_TAKEN_DETAIL`. The claim cannot be released without LiveKit.

- [ ] **Step 1: Write the failing tests** (service, using plan 1's fixtures `pbx_connection`, `fake_lk`):
  - `test_add_number_takes_over_other_workspace_unverified_claim`: org B adds an e164 that org A holds unverified. Then:
    - A's row is `released`;
    - `fake_lk.remove_inbound_number` was called with A's trunk and the e164;
    - B's row is `unverified` on B's trunk;
    - an audit row `number.claim_released` exists for org A.
  - `test_add_number_takes_over_own_unverified_claim`: same org, second connection. Same result, and no hang (`asyncio.wait_for(..., 5)`).
  - `test_active_claim_does_not_give_way`: A's row `active`. B gets `ConnectedNumberError.code == "number_taken"`, and A's row is unchanged.
  - `test_claim_with_running_check_does_not_give_way`: seed a check call for A's number inside `CHECK_WINDOW`. B gets `code == "check_in_progress"`; no trunk call; A unchanged.
  - `test_give_way_livekit_failure_keeps_claim`: `fake_lk.remove_inbound_number` raises. B gets `code == "livekit_failed"`. A's row is still `unverified`, and B has no row.
  - `test_purchase_takes_over_unverified_claim` (route): `POST /v1/numbers` with a quote for the claimed e164.
    - Expected: 201 (or today's success code), the claim released, the new row `pending`.
  - `test_purchase_takes_over_own_unverified_claim` (route): same, with buyer org = claim org.
  - `test_purchase_blocked_by_running_check` (route): 409, body detail "This number is being checked. Try again in 2 minutes.", no credit row, no number row.
  - `test_release_claim_ops_command`: `main(["release-claim", "+14155550100"])` returns 0 and releases the claim; an active row returns 1 with "not an unchecked claim".
- [ ] **Step 2: Run** `cd api && uv run pytest tests/test_connected_numbers.py -k "give_way or claim" tests/test_number_orders.py -k claim -v`. Expected: FAIL (`give_way_unchecked_claim` not defined).
- [ ] **Step 3: Implement** as in Interfaces.
- [ ] **Step 4: Run** the same command and the full `uv run pytest tests/test_connected_numbers.py tests/test_number_orders.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(numbers): an unchecked connected-number claim gives way to a purchase or another connection`.

### Task A2: Contacts take an extension

**Files:**

- Modify: `core/hailhq/core/schemas.py`:
  - `ContactCreate`, `ContactPatch`, `ContactEntry` get `extension: str | None`, pattern `^[0-9*#]{1,6}$`.
  - The `ContactCreate` validator becomes "phone, email or extension".
- Modify: `core/hailhq/core/contacts.py` (`contact_to_entry` copies `extension`), `api/hailhq/api/routes/contacts.py` (create and patch write it). A patch that would leave no phone, email or extension: 422 "a contact needs a phone, an email or an extension".
- Modify: `mcp/hailhq/mcp/tools.py` and `mcp/hailhq/mcp/hail_client.py`. `create_contact` gets `extension: str | None = None`, described as "PBX extension (1 to 6 digits, \* or #) for handover on a connected phone system".
- Modify: `cli/internal/cmd/contacts.go`: `create` and `update` get `--extension`.
- Test: `api/tests/test_contacts_api.py` (or the existing contacts route test), `mcp/tests/test_tools.py` (existing create_contact test file), `cli/internal/cmd/contacts_test.go`

**Interfaces:**

- Consumes: `Contact.extension` and the DB CHECKs from plan 1.
- Produces: `ContactEntry.extension: str | None` (read by the console contact list in Part B).

- [ ] **Step 1: Write failing tests:**
  - `test_create_contact_with_extension_only`: POST `{"name": "Support line", "extension": "800"}`. Expected: 201, `extension == "800"`, `phone_e164 is None`.
  - `test_create_contact_rejects_long_extension`: `"1234567"` gives 422.
  - `test_patch_contact_cannot_clear_all_reachability`: a contact with only an extension, PATCH `{"extension": null}`. Expected: 422.
  - MCP `test_create_contact_passes_extension`.
  - Go `TestContactsCreateExtension`: the request body holds `"extension":"800"`.
- [ ] **Step 2: Run** `cd api && uv run pytest tests -k contact -v`, `uv run --package hailhq-mcp --extra dev pytest mcp/tests -k contact`, `cd cli && go test ./internal/cmd -run Contacts`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same commands. Expected: PASS.
- [ ] **Step 5: Commit** `feat(contacts): a contact can be a phone system extension`.

### Task A3: Number fields `source` and `sip_connection_id`

**Files:**

- Modify: `core/hailhq/core/schemas.py`, `PhoneNumberResponse`:
  - `source: Literal["purchased", "connected"]`. Computed: `"connected"` when `provider == "pbx"`, else `"purchased"`. Add a `source` property on `PhoneNumber` in `core/hailhq/core/models.py`, so `from_attributes` reads it.
  - `sip_connection_id: UUID | None`.
  - The `provisioning_state` description gets `'unverified'` ("a connected number waiting for its ownership check").
- Modify: `sdk/hail/models.py` `PhoneNumberResponse`: the same two fields, with defaults (`source: str = "purchased"`, `sip_connection_id: UUID | None = None`), so an old API still parses.
- Modify: MCP tool descriptions of `list_numbers`, `get_number`, `route_number`, `release_number` in `mcp/hailhq/mcp/tools.py`:
  - one sentence each: "source 'connected' is a number on the customer's own phone system";
  - `route_number`: "it must pass its ownership check first";
  - `release_number`: "on a connected number this removes it from Hail only".
  - When a user asks to connect a phone system, the agent tells them to use the console (spec section 8).
- Test: `core/tests/test_schemas.py` (or the existing numbers schema test), `api/tests/test_numbers_api.py`, `sdk/tests/test_models.py`, `mcp/tests/test_output_schemas.py`

- [ ] **Step 1: Write failing tests:**
  - `test_number_response_connected_source`: a `pbx` row gives `source == "connected"`, `sip_connection_id == connection.id`.
  - `test_number_response_purchased_source`: a twilio row gives `"purchased"`, `None`.
  - SDK `test_phone_number_without_source_defaults_purchased`.
  - MCP: the `get_number` output schema lists `source`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** them, plus `uv run --package hailhq-mcp --extra dev pytest mcp/tests`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(numbers): numbers show whether they are bought or connected`.

### Task A4: `/v1/sip-connections` routes

**Files:**

- Create: `api/hailhq/api/routes/sip_connections.py`. Register it in `_CUSTOMER_ROUTERS` in `api/hailhq/api/main.py`.
- Modify: `core/hailhq/core/config.py` (`livekit_sip_host: str = ""`), `.env.example` (under "LiveKit Cloud": `# Host a customer's phone system sends calls to (connected numbers), shown in the console and API. Empty = connected numbers are off.` then `LIVEKIT_SIP_HOST=`), the main checkout `.env` (real value, same place).
- Modify: `core/hailhq/core/schemas.py` (models below).
- Test: `api/tests/test_sip_connections_api.py`

**Interfaces:**

- Consumes: plan 1's service functions (plan 1, "Interfaces for plan 2"), `CheckResult`, `ConnectedNumberError`.
- Produces (schemas in `core/hailhq/core/schemas.py`):

```python
class SipConnectionCreate(BaseModel):  # extra="forbid"
    label: str                      # 1-80 chars
    default_region: str             # ISO alpha-2
    pbx_host: str | None = None
    pbx_port: int = 5060
    pbx_transport: Literal["auto", "udp", "tcp", "tls"] = "auto"
    pbx_username: str | None = None
    pbx_password: str | None = None

class SipConnectionPatch(BaseModel):  # extra="forbid"; omitted = unchanged, null clears
    label: str | None
    pbx_host: str | None
    pbx_port: int | None
    pbx_transport: Literal["auto", "udp", "tcp", "tls"] | None
    pbx_username: str | None
    pbx_password: str | None

class SipConnectionResponse(BaseModel):
    id: UUID
    label: str
    default_region: str
    sip_host: str                   # settings.livekit_sip_host
    username: str                   # inbound_username
    password_last4: str
    pbx_host: str | None
    pbx_port: int
    pbx_transport: str
    pbx_username: str | None
    pbx_password_set: bool
    created_at: datetime

class SipConnectionCreated(SipConnectionResponse):
    password: str                   # shown once

class SipConnectionPassword(BaseModel):
    password: str                   # shown once

class SipConnectionListResponse(BaseModel):
    items: list[SipConnectionResponse]
    next_cursor: str | None

class ConnectedNumberCreate(BaseModel):  # extra="forbid"
    e164: str

class ConnectedNumberCheck(BaseModel):
    state: Literal["active", "unverified"]
    reason: Literal["no_call_arrived", "not_authenticated", "code_not_received"] | None
    call_id: UUID | None
```

Routes (all org-scoped; another org's id gives 404):

| Route                                                   | Success                                                                                            | Service call                                                          |
| ------------------------------------------------------- | -------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| `POST /sip-connections`                                 | 201 `SipConnectionCreated`                                                                         | `create_connection`                                                   |
| `GET /sip-connections`                                  | 200 `SipConnectionListResponse` (`limit` 1-100, default 50; `cursor`; newest first; skips deleted) | query                                                                 |
| `GET /sip-connections/{id}`                             | 200 `SipConnectionResponse`                                                                        | query                                                                 |
| `PATCH /sip-connections/{id}`                           | 200 `SipConnectionResponse`                                                                        | `update_connection` (only fields present in the body; others `UNSET`) |
| `POST /sip-connections/{id}/rotate-password`            | 200 `SipConnectionPassword`                                                                        | `rotate_inbound_password`                                             |
| `DELETE /sip-connections/{id}`                          | 204                                                                                                | `delete_connection`                                                   |
| `POST /sip-connections/{id}/numbers`                    | 201 `PhoneNumberResponse`                                                                          | `add_connected_number` (which gives way, Task A1)                     |
| `POST /sip-connections/{id}/numbers/{number_id}/verify` | 200 `ConnectedNumberCheck`                                                                         | `check_connected_number`                                              |
| `DELETE /sip-connections/{id}/numbers/{number_id}`      | 204                                                                                                | `remove_connected_number`                                             |

Error map, one function `_http_error(exc: ConnectedNumberError) -> HTTPException`. The detail is the exception message, which must read as customer copy; reword plan 1 messages that do not.

| Code                                                                                                                                | Status |
| ----------------------------------------------------------------------------------------------------------------------------------- | ------ |
| `invalid_input`, `invalid_number`, `host_not_allowed`, `password_required`, `not_callable`                                          | 422    |
| `number_taken`, `has_numbers`, `connection_deleted`, `not_unverified`, `check_in_progress`, `too_many_unverified`, `pool_exhausted` | 409    |
| `check_limit`                                                                                                                       | 429    |
| `no_funds`                                                                                                                          | 402    |
| `not_found`                                                                                                                         | 404    |
| `livekit_failed`                                                                                                                    | 502    |
| `not_configured` (also: `settings.livekit_sip_host` empty or no LiveKit client)                                                     | 503    |

No `idempotency_dep` on create or rotate (Review Focus 4). The audit payload of create, rotate and delete holds the id and label only.

- [ ] **Step 1: Write failing tests** in `api/tests/test_sip_connections_api.py` (fixture `fake_lk` from plan 1; `settings.livekit_sip_host` monkeypatched to `"example.sip.livekit.cloud"`):
  - `test_create_connection_returns_password_once`: 201. Expected:
    - `len(password) >= 24`;
    - `password_last4 == password[-4:]`;
    - `sip_host == "example.sip.livekit.cloud"`.
  - `test_create_connection_password_only_in_create_response`: create with an `Idempotency-Key` header. Expected:
    - no `idempotency_keys` row;
    - GET list and GET one: `"password" not in body`.
  - `test_connections_are_org_scoped`: org B GET, PATCH, DELETE on A's connection all give 404.
  - `test_patch_clears_pbx_host`: `{"pbx_host": null}` gives `pbx_host is None`, and the outbound trunk is deleted (`fake_lk.delete_trunk` called).
  - `test_rotate_password_returns_new_password`: a new value, and `password_last4` changes on GET.
  - `test_delete_connection_with_numbers_409`.
  - `test_add_number_then_verify_passes`: monkeypatch `check_connected_number` to `CheckResult("active", None, call_id)`. Expected: 200 `{"state": "active", "reason": null, ...}`.
  - `test_verify_failure_reason_is_200`: `CheckResult("unverified", "no_call_arrived", id)`. Expected: 200 with that reason.
  - `test_verify_limit_429`: the service raises `check_limit`. Expected: 429.
  - `test_routes_503_without_sip_host`: `livekit_sip_host = ""` makes `POST /sip-connections` give 503.
  - `test_remove_number_route`: 204, and the number is `released`.
- [ ] **Step 2: Run** `cd api && uv run pytest tests/test_sip_connections_api.py -v`. Expected: FAIL (404s).
- [ ] **Step 3: Implement** the schemas, the routes and the setting. Add the `.env.example` lines and the `.env` value, then run the parity diff:
      `diff <(sed -E 's/^([A-Z][A-Z0-9_]*)=.*/\1=/' .env) <(sed -E 's/^([A-Z][A-Z0-9_]*)=.*/\1=/' .env.example)` prints nothing (run it in the main checkout).
  - The worktree's own `.env` gets the same key.
- [ ] **Step 4: Run** the test file and the full API suite `cd api && uv run pytest -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(api): /v1/sip-connections routes for connected phone systems`.

### Task A5: OpenAPI, Go client, CLI commands

**Files:**

- Modify: `openapi/openapi.yaml`. Run the API, then the command in `docs/public/contributing.md` (curl `/openapi.json` piped to `yaml.safe_dump`).
- Modify: `cli/internal/client/client.gen.go` (`cd cli && make codegen`)
- Create: `cli/internal/cmd/sip_connections.go`, `cli/internal/cmd/sip_connections_test.go`; register it in `cli/internal/cmd/root.go`
- Modify: `cli/internal/cmd/number.go` (list output shows `source`; the `release` help says it removes a connected number from Hail only)

Commands (spec section 8 names):

```bash
hail sip-connections create --label "Main office" --region US [--pbx-host pbx.example.com --pbx-port 5060 --pbx-transport auto --pbx-username u --pbx-password p]
hail sip-connections list
hail sip-connections get <id>
hail sip-connections update <id> [--label ... --pbx-host ... --clear-pbx-host ...]
hail sip-connections rotate-password <id>
hail sip-connections delete <id>
hail sip-connections add-number <id> +14155550100
hail sip-connections verify <id> <number_id>
hail sip-connections remove-number <id> <number_id>
```

- `create` and `rotate-password` print the password with "shown once" on stderr. `--json` prints the full body.
- `verify`:
  - before the request, prints "Calling the number to check it. This can take up to 2 minutes." to stderr;
  - prints `Connected` (exit 0), or the reason in words (exit 1):
    - `no_call_arrived`: "No call arrived. Check the SIP trunk and the routing rule.";
    - `not_authenticated`: "The call came in without this connection's username and password.";
    - `code_not_received`: "The check code did not come through. Turn on RFC 2833 key presses on the trunk.".

- [ ] **Step 1: Write failing Go tests** (httptest server, like `contacts_test.go`):
  - `TestSipConnectionsCreatePrintsPasswordOnce`;
  - `TestSipConnectionsVerifyConnected` (exit 0);
  - `TestSipConnectionsVerifyNoCallArrived` (exit 1, message above);
  - `TestSipConnectionsVerifyNoShortTimeout`: the server sleeps 3 s and the command still succeeds. This also guards that no client timeout below 120 s is added.
- [ ] **Step 2: Run** `cd cli && go test ./internal/cmd -run SipConnections`. Expected: FAIL.
- [ ] **Step 3: Regenerate** OpenAPI and the client, then implement the commands.
- [ ] **Step 4: Run** `cd cli && go test ./... && gofmt -l .` (no output), and the OpenAPI check `.github/workflows/openapi-check.yml` steps locally. Expected: PASS.
- [ ] **Step 5: Commit** `feat(cli): hail sip-connections commands`.

### Task A6: Docs and changelog

**Files:**

- Create: `docs/public/connect-phone-system.md`, added to `docs/public/meta.json` after `agents`. Content:
  - one runnable curl flow (spec section 8, steps 1 to 4);
  - the generic SIP trunk fields;
  - number format `+E.164`;
  - audio G.711 (PCMU or PCMA);
  - key presses RFC 2833 / RFC 4733;
  - send calls straight to the trunk (no queue or IVR that answers first);
  - calls in use (a call to Hail uses 2 of the phone system's channels, a handover 1 more);
  - the shared source IP warning: send trunk calls only to your own extensions;
  - the check: what it does, its cost (a normal outbound call), 5 a day;
  - an unchecked number is removed after 7 days, or when someone else buys or connects it;
  - handover to extensions (contacts with `extension`);
  - price 10¢ a minute.
  - Links: OpenAPI operations, `docs/public/cli.md`, `docs/public/mcp.md`. Fits one screen plus the code block.
- Modify:
  - `docs/public/cli.md` (the `sip-connections` commands);
  - `docs/public/mcp.md` (the `source` field; connecting runs in the console; `create_contact` `extension`);
  - `docs/public/agents.md` (handover contact may be an extension on a connected number, one line);
  - `docs/public/self-host/livekit-cloud.md` (`LIVEKIT_SIP_HOST`);
  - `docs/operations/connected-numbers.md` (the public routes now exist; the console is the main path);
  - `CHANGELOG.md` `[Unreleased]` → `### Added`: "Connect your phone system: keep your number and phone system, and send its calls to a Hail agent." and "Contacts can be phone system extensions.".
- Modify: `mcp/server.json` (version bump; the release PR after merge publishes it).

- [ ] **Step 1: Write the docs.** Then run the docs-site build or link check, if the repo has one (`grep -n "docs" package.json`). Expected: no broken links.
- [ ] **Step 2: Commit** `docs: connect your phone system`.

### Task A7: Ops before the PR is ready (I run these; nothing for the owner)

- [ ] Check the prod LiveKit dispatch rule `hail-inbound`: `trunk_ids` is empty (spec ops step 3). If not, clear it with `scripts/ops/sip_trunks.py` as `docs/operations/connected-numbers.md` says, and record it in the chat, not the repo.
- [ ] Run the CI steps myself (GitHub Actions minutes are out): ruff, black, the pytest suites of `api`, `core`, `mcp`, `voicebot`, `sdk`, the Go tests and the OpenAPI check. Run them on the branch merged with the latest `origin/main`, and put commands and results in the PR body.
- [ ] Open the PR. PR body:
  - What merge does: deploy via GHA, no migration.
  - What it does not do: the VM `.env` needs `LIVEKIT_SIP_HOST`. Give the copy step and the restart.
  - Merge order: this PR, deploy, then hail-website Part B.

---

# Part B: hail-website, two PRs (not stacked; both from `master`)

## PR B1: console panel (merges after Part A is deployed)

Branch `feat/pbx-connect-console`, worktree `hail-website/.claude/worktrees/feat-pbx-connect-console`. Draft PR until done.

### Task B1: Numbers list knows connected numbers

**Files:**

- Modify: `lib/sms-queries.ts` `getNumbers`:
  - also select `n.provider = 'pbx' AS connected`, `n.sip_connection_id`, and the connection label (`LEFT JOIN sip_connections`);
  - widen the state filter to include `'unverified'` for `pbx` rows only.
- Modify: `app/console/numbers/NumbersPanel.tsx`. For a connected number:
  - a "Connected" tag and the connection label;
  - state `unverified` shows the yellow wait tag "Checking", with a "Check" button (`Button kind="primary" size="sm"`);
  - state `active` shows "Connected";
  - "Added <TimeAgo>" instead of "Bought";
  - "Remove" (danger) instead of "Release", confirm text: "Remove {e164} from Hail? Calls stop reaching your agents. The number stays with your phone system and provider.";
  - the header count counts active connected numbers too.
- Test: `app/console/numbers/__tests__/NumbersPanel.test.tsx`

- [ ] **Step 1: Failing tests:**
  - `shows Checking and a Check button for an unverified connected number`;
  - `shows Remove, not Release, for a connected number`;
  - `a bought number still shows Release and Bought`.
- [ ] **Step 2: Run** `pnpm vitest run app/console/numbers`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** it. Expected: PASS.
- [ ] **Step 5: Commit** `feat(console): numbers list shows connected numbers`.

### Task B2: "Connect your phone system" panel

**Files:**

- Create: `app/console/numbers/ConnectPhoneSystemPanel.tsx`, `app/console/numbers/connect-actions.ts` (`"use server"`, through `orgApiAction` and `callHailApiAsOrg`)
- Modify: `app/console/numbers/page.tsx`:
  - two choices at the top, "Buy a number" and "Connect your phone system" (`Button look="choice"`);
  - `export const maxDuration = 120`.
- Test: `app/console/numbers/__tests__/ConnectPhoneSystemPanel.test.tsx`, `app/console/numbers/__tests__/connect-actions.test.ts`

**Interfaces (server actions, each returns `{ ok: true, ... } | { ok: false, error: string }`):**

```ts
createConnection(input: { label: string; defaultRegion: string; pbxHost?: string }): Promise<Result<{ connection: SipConnection; password: string }>>
addConnectedNumber(connectionId: string, e164: string): Promise<Result<{ number: PhoneNumber }>>
verifyConnectedNumber(connectionId: string, numberId: string): Promise<Result<{ state: "active" | "unverified"; reason: CheckReason | null }>>  // fetch signal AbortSignal.timeout(115_000)
updateConnection(connectionId: string, patch: { pbxHost?: string | null; pbxUsername?: string | null; pbxPassword?: string | null }): Promise<Result<{ connection: SipConnection }>>
rotateConnectionPassword(connectionId: string): Promise<Result<{ password: string }>>
removeConnectedNumber(connectionId: string, numberId: string): Promise<Result<{}>>
```

Steps on screen (spec section 8 mock-up). Copy:

1. "Name it" (label) and Country (`select` with the shared chevron).
2. "Add this SIP trunk to your phone system". Rows, each value with `CopyButton iconOnly` in `.c-copy-value`:
   - SIP server;
   - Username;
   - Password, with the hint "Copy it now. It shows only once." and a quiet "New password" button;
   - Audio: G.711 (PCMU or PCMA);
   - Number format: +country code (for example +14155550100);
   - Key presses: RFC 2833.
3. "Your number": `PhoneInput`, on its own row.
4. "Send this number's calls to Hail, then check it." Primary "Check".
   - While running: "Checking… this takes up to 2 minutes."
   - On pass: green "Connected".
   - On fail, the reason in words (same three lines as the CLI). Never a raw code.
5. "Calls out and handover (optional)": "Your phone system's address" (host), optional username and password.
6. "Choose the agent": reuses the existing agent select of the numbers list.

- Errors from the API show as one line under the action.
- The panel never stores the password in state after the person leaves step 2.

- [ ] **Step 1: Failing tests:**
  - `shows the password once and hides it after Continue`;
  - `Check shows Connected on active`;
  - `Check shows the no-call reason in words`;
  - `verifyConnectedNumber passes an abort signal of 115 s` (mock `callHailApiAsOrg`, assert `init.signal`).
- [ ] **Step 2: Run** `pnpm vitest run app/console/numbers`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** it, then `pnpm run lint` and `pnpm run build`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(console): connect your phone system`.

### Task B3: Contacts and calls

**Files:**

- Modify: `app/console/contacts/ContactsClient.tsx`, `app/console/contacts/actions.ts`: an "Extension" box (1 to 6 characters: digits, \* or #), hint "For handover on your phone system". A contact may have only an extension.
- Modify: the call list and the call drawer (`app/console/activity/`, the component that shows a call's number). A call on a `pbx` number shows "Your phone system" next to the number.
- Modify: `lib/agent-skill-doc.ts`: review it for numbers and contacts (AGENTS.md "Product facts"). Change it only if a claim there is now wrong.
- Test: the existing contacts and activity tests: `contact with only an extension saves`, `call on a connected number shows Your phone system`.

- [ ] Steps 1-5 as above. Commit `feat(console): extension contacts and connected-number calls`.

### Task B4: UI checks before hand-off

- [ ] Run the local backend preview (`docs/development/local-backend-preview.md`) with Part A's branch. Then walk the real panel signed in:
  - create a connection;
  - add the test DID;
  - run Check through the owner's test PBX trial (kept from plan 1);
  - route the number to an agent;
  - remove it.
- [ ] Measure alignment with Playwright `boundingBox()` at 1500, 1024, 390 and 320 px (owner rules). Fill long values: a 253-character host and a long label. Check focus rings, light and dark.
- [ ] `pnpm run build`; open the PR as draft, then `gh pr ready` when done.

## PR B2: pricing line (merges after PR B1)

Branch `feat/pbx-pricing-line` from `master`.

### Task B5: One pricing line

**Files:**

- Modify: `app/(marketing)/pricing/page.tsx`. One line in the voice section: "Connect your phone system: {callCentsPerMinute}¢ a minute, no monthly fee". The value comes from `providerPolicy("pbx")`.
- Test: `app/(marketing)/pricing/__tests__/pricing-estimate.test.tsx` (or a new `pricing-pbx-line.test.tsx`): `renders the connected phone system price from PROVIDER_POLICIES`.
  - Change `callCentsPerMinute` in the test's mocked policy to 12.
  - Expected: "12¢ a minute".

- [ ] Steps 1-5 as above. Commit `feat(pricing): connect your phone system price line`.
- [ ] Screenshot `/pricing` at 1500 and 390 px, light and dark.

---

# After merges (I do these)

1. Part A merged and deployed:
   - check `/healthz`;
   - `GET /v1/sip-connections` with a test key gives 200.
2. Release PR for CLI, SDK and MCP right after the Part A merge ("Cutting a repo release" in `docs/public/self-host/operations.md`).
3. Part B1 merged:
   - one signed-in console pass on prod with the owner's test PBX;
   - remove the test connection after.
4. Part B2 merged: check the live `/pricing` line.
5. Cleanup:
   - worktrees and branches;
   - the test DB compose project `hail-pbx-connected-numbers-api`;
   - the local preview resources.
   - The owner's test PBX trial and test numbers stay until the owner says the feature is done.
