# Connected numbers (backend and billing) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A customer's phone system (PBX) can send calls to a Hail agent over its own SIP trunk pair, the agent can call out and hand over through it, and every such call is billed at the connected-number price. No public route creates a connection yet.

**Architecture:** A new `sip_connections` table holds one LiveKit inbound and one outbound trunk per connection; a connected number is a `phone_numbers` row with `provider = 'pbx'`. Routing takes the number (not the carrier name), so a `pbx` number resolves to its connection's trunks. Billing stays in hail-website: one `pbx` price entry, read by the rater and the quote route.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, Alembic, pytest, `livekit-api`, `phonenumbers`; TypeScript, Next.js, Vitest (hail-website).

**Spec:** `docs/superpowers/specs/2026-11-18-pbx-connected-numbers-design.md`

## Global Constraints

- Provider value for connected numbers: `pbx`. Never added to `CARRIERS` (`core/hailhq/core/carrier_routing.py`).
- New `phone_numbers.provisioning_state` value: `unverified`. Never `pending` (the order reconciler reads `pending`).
- A `pbx` row: `capabilities = ['voice']`, `is_pool = false`, `provider_resource_id = NULL`, `sip_connection_id` set.
- CHECK `(provider = 'pbx') = (sip_connection_id IS NOT NULL)`; FK `ON DELETE RESTRICT`.
- `sip_connections.label`: 1 to 80 characters. `pbx_port` default 5060. `pbx_transport` in `auto, udp, tcp, tls`, default `auto`.
- Inbound password: at least 24 random characters. Username: Hail generates it, unique.
- Secrets: Fernet with `HAIL_PROVIDER_SECRET_KEY` (`provider_cipher()` in `core/hailhq/core/provider_config.py`). No new env var.
- Inbound trunk `numbers` always holds the placeholder `pbx-<connection id>`. Never send a `set` or an empty list for a connection trunk.
- Dialed number must be `+E.164`. A caller of 1 to 6 digits stays as given, without `+`.
- `contacts.extension`: 1 to 15 characters of digits, `*` or `#`.
- Ownership check: ring limit 30 s, a 6-digit code sent as key presses (only the first 6 digits received count), check window and check call max duration 90 s, at most 5 checks per number per rolling 24 h, `unverified` rows older than 7 days are removed.
- Connected call price (owner, 2026-11-18): 10 cents a minute of call length, every direction and country. Each answered handover leg adds 10 cents a minute from its answer to its end, so a 2-person handover costs 20 cents a minute while the person is on the line. Both are pro rata by the millisecond, like the call price today. Recording adds 1 cent a minute of call length. Monthly fee 0; setup fee 0. hail-website `lib/private-rates.ts`: `PBX_PRICES.cents_per_minute = 10`, `PBX_PRICES.handover_cents_per_minute = 10`.
- The handover leg's time comes from its `call_legs` row (`role = 'handover'`, `answered_at`, `left_at`), which the voicebot already writes for every SIP leg before the call's usage event. No new column.
- Ownership check call: the normal outbound price for the destination (a carrier call from a pool number).
- `call_legs` rows of a `pbx` call: `provider = 'pbx'`, `cost_status = 'found'`, `cost_usd = 0` at insert.
- Migration revision: the next free revision after main's head, `down_revision` set to that head. The docstring names the tables checked (CLAUDE.md "Schema changes" rule 4).
- Tests need local Postgres: `docker compose -f docker-compose.yml -f docker-compose.local.yml up -d postgres`, then `export DATABASE_URL=postgresql+psycopg://hail:hail@localhost:5432/hail`.
- Python checks before each commit: `uvx ruff==0.16.10 check .` and `uvx black==26.10.0 --check .` from the repo root.
- Commits: Conventional Commits. No `Co-Authored-By` or other AI attribution line.
- Public repo: neutral examples only (`+14155550100`, `+442079460000`, `+447400123456`, "Main office", `pbx.example.com`). No hosts, paths or account names from the deployment.
- No public API route, OpenAPI change, CLI, MCP or console work in this plan. `ContactCreate` and `ContactPatch` do not change.
- Order: Part A merges and deploys first. Part B merges after. No `pbx` row may exist in production before Part B is deployed.

## Review Focus

1. **A connection trunk becomes a wildcard.** LiveKit treats an inbound trunk with an empty `numbers` list as "any number". Removing the last number, or a `set` update, must never drop the placeholder. Pinned by `test_remove_inbound_number_refuses_placeholder` (Task A2) and `test_remove_last_number_keeps_placeholder` (Task A7).
2. **A false pass on the ownership check.** An INVITE on the wrong trunk, an INVITE with no check in flight, an INVITE after the check window, or an INVITE without the right code must never set the number `active`. Only the first 6 key presses count (no sliding window over many digits). Pinned by `test_check_arrival_on_other_trunk_does_not_activate`, `test_arrival_without_check_in_flight_is_dropped`, `test_arrival_after_window_is_dropped`, `test_right_code_activates`, `test_wrong_or_missing_code_does_not_activate` and `test_only_first_six_digits_count` (Task A12).
3. **A `pbx` value reaching carrier-only code.** `carrier('pbx')` raises, so a `pbx` leg left `pending` loops in `fetch_leg_costs` for 48 h, and the hail-website rater would price a `pbx` leg from another carrier's rate table. Pinned by `test_pbx_legs_are_found_at_zero` (Task A9) and `it("never prices a pbx leg from the rate table")` (Task B2).
4. **A removed connected number in the monthly fee run.** "Remove" tombstones the row as `released` with `released_at`, and the monthly query selects released rows. With no price, the run counts it unpriced and answers 500. Pinned by `it("skips pbx numbers, active and removed")` (Task B4).
5. **An extension or anonymous caller.** `101` must stay `101` (today it becomes `+101`), and the handover "same person on the line" check must still match an extension. Pinned by `test_normalize_caller_keeps_extension` (Task A8) and `test_handover_refuses_extension_on_the_line` (Task A11).
6. **The handover leg on a connected call goes unbilled.** The leg costs Hail 0, but the customer pays 10 cents a minute for it. Pinned by `test_pbx_legs_are_found_at_zero` (Task A9: the row keeps `answered_at` and `left_at`), `it("charges an answered pbx handover 10 cents a minute on top")` (Task B2) and `it("quotes a pbx handover at 20 cents a minute")` (Task B3).

---

# Part A: hail PR 1

Branch: `feat/pbx-connected-numbers`.

## Task A1: Migration and models

**Files:**
- Create: `api/migrations/versions/<next>_sip_connections.py`
- Modify: `core/hailhq/core/models.py` (`Contact` 118-172, `PhoneNumber` 631-775, `CallLeg` 944-1025; new `SipConnection` class after `PhoneNumber`)
- Test: `api/tests/test_sip_connections_migration.py`, `core/tests/test_models.py`

**Interfaces:**
- Produces: `SipConnection` ORM model (table `sip_connections`); `PhoneNumber.sip_connection_id: Mapped[uuid.UUID | None]`; `Contact.extension: Mapped[str | None]`.
- `SipConnection` columns, exactly as spec section 1, with two type choices: `encrypted_inbound_password: Mapped[str]` and `encrypted_pbx_password: Mapped[str | None]` are `Text` (Fernet tokens are `str`, as `org_provider_config.encrypted_api_key`).
- Constraints:
  - `sip_connections_label_len`: `char_length(label) BETWEEN 1 AND 80`
  - `sip_connections_transport_check`: `pbx_transport IN ('auto','udp','tcp','tls')`
  - `sip_connections_port_check`: `pbx_port BETWEEN 1 AND 65535`
  - `sip_connections_inbound_username_key`: UNIQUE `inbound_username`
  - `sip_connections_org_idx`: index `organization_id` WHERE `deleted_at IS NULL`
  - `phone_numbers_state_check`: `provisioning_state IN ('pending','active','failed','released','unverified')`
  - `phone_numbers_pbx_connection_check`: `(provider = 'pbx') = (sip_connection_id IS NOT NULL)`
  - `phone_numbers_sip_connection_idx`: index `sip_connection_id` WHERE `sip_connection_id IS NOT NULL`
  - `call_legs_provider_check`: `provider IN ('twilio','telnyx','didww','pbx')`
  - `contacts_phone_email_or_extension` replaces `contacts_phone_or_email`: `phone_e164 IS NOT NULL OR email IS NOT NULL OR extension IS NOT NULL`
  - `contacts_extension_format`: `extension ~ '^[0-9*#]{1,15}$'`

- [ ] **Step 1: Write the failing tests.** Migration round trip in the style of `api/tests/test_call_balance_holds_migration.py` (`pytestmark = pytest.mark.recreate_schema`, load the file with `importlib`, run `downgrade()` then `upgrade()`):

```python
async def test_sip_connections_migration_round_trip(db):
    # after downgrade() + upgrade():
    assert columns("sip_connections") == [
        "created_at", "default_region", "deleted_at", "encrypted_inbound_password",
        "encrypted_pbx_password", "id", "inbound_username", "label",
        "livekit_inbound_trunk_id", "livekit_outbound_trunk_id", "organization_id",
        "pbx_host", "pbx_port", "pbx_transport", "pbx_username", "updated_at",
    ]
    assert "sip_connection_id" in columns("phone_numbers")
    assert "extension" in columns("contacts")
    assert fk_delete_rule("phone_numbers", "sip_connections") in (b"r", "r")
```

  ORM constraint tests in `core/tests/test_models.py` (`async_session` fixture):

```python
async def test_pbx_number_needs_connection(async_session):
    async_session.add(pbx_row("+14155550100", sip_connection_id=None))
    with pytest.raises(IntegrityError, match="phone_numbers_pbx_connection_check"):
        await async_session.flush()

async def test_carrier_number_cannot_have_connection(async_session, conn):
    async_session.add(number_row("+14155550100", provider="twilio", sip_connection_id=conn.id))
    with pytest.raises(IntegrityError, match="phone_numbers_pbx_connection_check"):
        await async_session.flush()

async def test_unverified_state_is_allowed_and_holds_e164(async_session, conn):
    first = pbx_row("+14155550100", conn.id, provisioning_state="unverified")
    async_session.add(first)
    await async_session.commit()
    assert first.provisioning_state == "unverified"
    async_session.add(pbx_row("+14155550100", conn.id, provisioning_state="unverified"))
    with pytest.raises(IntegrityError, match="phone_numbers_e164_live_uniq"):
        await async_session.flush()

async def test_contact_with_only_extension(async_session):
    ok = Contact(organization_id=ORG, name="Support line", extension="800")
    async_session.add(ok)
    await async_session.commit()
    assert ok.extension == "800"
    async_session.add(Contact(organization_id=ORG, name="Bad", extension="80a"))
    with pytest.raises(IntegrityError, match="contacts_extension_format"):
        await async_session.flush()
    await async_session.rollback()
    async_session.add(Contact(organization_id=ORG, name="Empty"))
    with pytest.raises(IntegrityError, match="contacts_phone_email_or_extension"):
        await async_session.flush()

async def test_call_leg_accepts_pbx(async_session, pbx_call):
    leg = CallLeg(call_id=pbx_call.id, role="call", provider="pbx", direction="inbound",
                  joined_at=T0, cost_status="found", cost_usd=Decimal("0"))
    async_session.add(leg)
    await async_session.commit()
    assert leg.provider == "pbx"
```

  `pbx_row(e164, sip_connection_id, **over)` and `number_row(...)` are small builders in the test file (provider `pbx`, `capabilities=["voice"]`, `country_code="US"`, `number_type="local"`).

- [ ] **Step 2: Run them.** `cd api && uv run pytest tests/test_sip_connections_migration.py -v` and `cd core && uv run pytest tests/test_models.py -v`. Expected: FAIL (no file, no columns).
- [ ] **Step 3: Implement.** Write `<next>_sip_connections.py` (the next free revision after main's head). Docstring: tables checked are `org_provider_config` (BYO keys for `llm`/`tts`/`stt` only, one row per org, layer and provider; the voicebot loads every row as pipeline settings) and `phone_numbers` (numbers, not credentials); neither fits, so a new table. Downgrade reverses each change and fails while `pbx` rows or extension-only contacts exist (the restored CHECKs reject them); say so in the docstring. Mirror every column and constraint in `models.py`.
- [ ] **Step 4: Run them.** Same commands. Expected: PASS. Also `cd api && uv run pytest tests/test_migrations.py -v`. Expected: PASS.
- [ ] **Step 5: Commit.**

```bash
git add api/migrations/versions/<next>_sip_connections.py core/hailhq/core/models.py \
  api/tests/test_sip_connections_migration.py core/tests/test_models.py
git commit -m "feat(db): sip_connections table and pbx number, leg and extension columns"
```

## Task A2: LiveKit trunk functions

**Files:**
- Modify: `core/hailhq/core/livekit.py` (after `remove_inbound_number`, line 216; `create_sip_participant` 138-167)
- Modify: `docs/public/self-host/livekit-cloud.md` (dispatch rule JSON, lines 90-106)
- Test: `core/tests/test_livekit.py`

**Interfaces:**
- Produces, on `LiveKitClient`:

```python
PBX_TRUNK_PREFIX = "pbx-"

def pbx_placeholder(connection_id: UUID) -> str  # "pbx-<connection id>"

async def create_pbx_inbound_trunk(
    self, connection_id: UUID, username: str, password: str
) -> str  # trunk id; name and numbers = [pbx_placeholder(connection_id)]

async def set_pbx_inbound_credentials(
    self, trunk_id: str, username: str, password: str
) -> None

async def create_pbx_outbound_trunk(
    self, connection_id: UUID, host: str, port: int, transport: str,
    username: str | None, password: str | None,
) -> str  # name f"pbx-{connection_id}-out", address f"{host}:{port}", numbers ["*"]

async def update_pbx_outbound_trunk(
    self, trunk_id: str, host: str, port: int, transport: str,
    username: str | None, password: str | None,
) -> None

async def delete_sip_trunk(self, trunk_id: str) -> None  # a 404 counts as done

async def create_sip_participant(..., wait_until_answered: bool = False,
    ringing_timeout_seconds: int | None = None) -> api.SIPParticipantInfo
```

- `remove_inbound_number` raises `ValueError` when the value starts with `PBX_TRUNK_PREFIX`.
- Transport map: `auto -> SIP_TRANSPORT_AUTO`, `udp -> SIP_TRANSPORT_UDP`, `tcp -> SIP_TRANSPORT_TCP`, `tls -> SIP_TRANSPORT_TLS`.

- [ ] **Step 1: Write the failing tests** (the `client` fixture mocks `LiveKitAPI`; add `create_sip_inbound_trunk`, `create_sip_outbound_trunk`, `update_inbound_trunk_fields`, `update_outbound_trunk_fields`, `delete_sip_trunk` as `AsyncMock`s):

```python
async def test_create_pbx_inbound_trunk_lists_only_the_placeholder(client):
    await client.create_pbx_inbound_trunk(CONN_ID, "hail-ab12", "p" * 32)
    (req,), _ = client._lkapi.sip.create_sip_inbound_trunk.call_args
    assert isinstance(req, api.CreateSIPInboundTrunkRequest)
    assert req.trunk.name == f"pbx-{CONN_ID}"
    assert list(req.trunk.numbers) == [f"pbx-{CONN_ID}"]
    assert req.trunk.auth_username == "hail-ab12"

async def test_create_pbx_outbound_trunk_dials_the_pbx(client):
    await client.create_pbx_outbound_trunk(CONN_ID, "pbx.example.com", 5060, "tls", None, None)
    (req,), _ = client._lkapi.sip.create_sip_outbound_trunk.call_args
    assert req.trunk.address == "pbx.example.com:5060"
    assert req.trunk.transport == api.SIPTransport.SIP_TRANSPORT_TLS
    assert list(req.trunk.numbers) == ["*"]
    assert req.trunk.auth_username == ""

@pytest.mark.parametrize("name,enum", [
    ("auto", api.SIPTransport.SIP_TRANSPORT_AUTO), ("udp", api.SIPTransport.SIP_TRANSPORT_UDP),
    ("tcp", api.SIPTransport.SIP_TRANSPORT_TCP), ("tls", api.SIPTransport.SIP_TRANSPORT_TLS),
])
async def test_transport_map(client, name, enum):
    await client.create_pbx_outbound_trunk(CONN_ID, "pbx.example.com", 5060, name, None, None)
    (req,), _ = client._lkapi.sip.create_sip_outbound_trunk.call_args
    assert req.trunk.transport == enum

async def test_set_pbx_inbound_credentials_changes_only_auth(client):
    await client.set_pbx_inbound_credentials("ST_1", "hail-ab12", "q" * 32)
    client._lkapi.sip.update_inbound_trunk_fields.assert_awaited_once_with(
        "ST_1", auth_username="hail-ab12", auth_password="q" * 32)   # no numbers kwarg

async def test_update_pbx_outbound_trunk_sets_address_and_auth(client):
    await client.update_pbx_outbound_trunk("ST_2", "pbx.example.com", 5061, "tcp", "pbxuser", "pw")
    client._lkapi.sip.update_outbound_trunk_fields.assert_awaited_once_with(
        "ST_2", address="pbx.example.com:5061", transport=api.SIPTransport.SIP_TRANSPORT_TCP,
        auth_username="pbxuser", auth_password="pw")

async def test_remove_inbound_number_refuses_placeholder(client):
    with pytest.raises(ValueError):
        await client.remove_inbound_number("ST_1", f"pbx-{CONN_ID}")
    client._lkapi.sip.update_inbound_trunk_fields.assert_not_awaited()

async def test_delete_sip_trunk_treats_404_as_done(client):
    client._lkapi.sip.delete_sip_trunk.side_effect = api.TwirpError("not_found", "gone", status=404)
    assert await client.delete_sip_trunk("ST_1") is None   # no raise
    client._lkapi.sip.delete_sip_trunk.assert_awaited_once()

async def test_create_sip_participant_can_wait_for_answer(client):
    await client.create_sip_participant("r", "+14155550100", "+14155550199", "ST_1", "check-1",
                                        wait_until_answered=True, ringing_timeout_seconds=30)
    (req,), _ = client._lkapi.sip.create_sip_participant.call_args
    assert req.wait_until_answered is True
    assert req.ringing_timeout.seconds == 30
```

- [ ] **Step 2: Run.** `cd core && uv run pytest tests/test_livekit.py -v`. Expected: FAIL on the new tests.
- [ ] **Step 3: Implement** the signatures above in `core/hailhq/core/livekit.py`. Check the `TwirpError` constructor in the installed `livekit-api` before writing the 404 test. `update_inbound_trunk_fields` and `update_outbound_trunk_fields` take `trunk_id` plus keyword fields (`livekit/api/sip_service.py`).
- [ ] **Step 4: Manual LiveKit probe (dev LiveKit project, not production).** Three checks:
  1. An inbound trunk whose `numbers` holds `pbx-<uuid>` (the placeholder) is accepted.
  2. An outbound trunk with `numbers: ["*"]` is accepted.
  3. A dial on that trunk with a `sip_number` that is on no trunk is accepted.

  Record each as yes/no in the PR body. If check 1 is "no", stop and report: the trunk design depends on it. If check 2 or 3 is "no", use the fallback and report it in the PR body:
  - The outbound trunk `numbers` becomes the connection's live numbers. Task A7 `add_connected_number` and `remove_connected_number` also update the outbound trunk.
  - Task A11 rule 6: the handover caller ID falls back to the connected number.
- [ ] **Step 5: Docs.** In `docs/public/self-host/livekit-cloud.md`, change the dispatch rule JSON to `"trunk_ids": []` and add two sentences: "An empty list matches every inbound trunk in the project, including the trunks Hail creates for each connected phone system. Keep the LiveKit project for Hail only."
- [ ] **Step 6: Run.** Same command. Expected: PASS.
- [ ] **Step 7: Commit.**

```bash
git add core/hailhq/core/livekit.py core/tests/test_livekit.py docs/public/self-host/livekit-cloud.md
git commit -m "feat(livekit): per-connection SIP trunks that never become a wildcard"
```

## Task A3: Connection helpers in core

**Files:**
- Create: `core/hailhq/core/sip_connections.py`
- Modify: `core/hailhq/core/carrier_routing.py` (constants, after `DIDWW = "didww"` line 40)
- Test: `core/tests/test_sip_connections.py`

**Interfaces:**
- Produces in `carrier_routing.py`: `PBX = "pbx"`.
- Produces in `sip_connections.py`:

```python
CHECK_RING_SECONDS = 30
CHECK_CODE_SECONDS = 15                   # how long the inbound leg waits for the 6-digit code
CHECKS_PER_DAY = 5
CHECK_WINDOW = timedelta(seconds=90)      # a check call newer than this, not terminal, is in flight
                                          # ring 30 + result wait 18 + line 6 = 54 s, under 90
UNVERIFIED_TTL = timedelta(days=7)
CHECK_CALL_META = "pbx_check_number_id"   # calls.metadata key on an ownership check call
CHECK_EVENT_KIND = "pbx_check"            # call_events.kind written by the inbound side

def new_inbound_username() -> str   # "hail-" + secrets.token_hex(6)
def new_password() -> str           # secrets.token_urlsafe(24): 32 characters
def number_shape(e164: str) -> tuple[str, str]  # (country_code, number_type); ValueError if not a valid number
def encrypt_secret(plain: str) -> str   # provider_cipher().encrypt
def decrypt_secret(token: str) -> str
async def connection_of(db: AsyncSession, number: PhoneNumber) -> SipConnection  # ValueError if missing or deleted
```

- `number_shape` type map (`phonenumbers.number_type`): `FIXED_LINE` and `FIXED_LINE_OR_MOBILE` -> `local`; `MOBILE` -> `mobile`; `TOLL_FREE` -> `toll_free`; anything else -> `national`.
- `sip_connections.py` never imports `carrier_routing`: Task A4 makes `carrier_routing` import `sip_connections`, and the reverse is an import cycle. Imports at the top of the file.

- [ ] **Step 1: Write the failing tests.**

```python
def test_password_is_long_enough():
    assert len(new_password()) >= 24
    assert new_password() != new_password()

def test_username_format():
    assert re.fullmatch(r"hail-[0-9a-f]{12}", new_inbound_username())

@pytest.mark.parametrize("e164,shape", [
    ("+14155550100", ("US", "local")),
    ("+442079460000", ("GB", "local")),
    ("+447400123456", ("GB", "mobile")),
    ("+18005550100", ("US", "toll_free")),
])
def test_number_shape(e164, shape):
    assert number_shape(e164) == shape

def test_number_shape_refuses_invalid():
    with pytest.raises(ValueError):
        number_shape("+1415")

def test_secret_round_trip(monkeypatch):
    monkeypatch.setattr(settings, "hail_provider_secret_key", generate_key())
    assert decrypt_secret(encrypt_secret("s3cret")) == "s3cret"

async def test_connection_of_returns_live_connection(async_session, conn, pbx_number):
    assert (await connection_of(async_session, pbx_number)).id == conn.id

async def test_connection_of_refuses_deleted_connection(async_session, conn, pbx_number):
    conn.deleted_at = T0
    await async_session.flush()
    with pytest.raises(ValueError):
        await connection_of(async_session, pbx_number)

async def test_connection_of_refuses_carrier_number(async_session, twilio_number):
    with pytest.raises(ValueError):
        await connection_of(async_session, twilio_number)
```

- [ ] **Step 2: Run.** `cd core && uv run pytest tests/test_sip_connections.py -v`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** the module and constant.
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit.**

```bash
git add core/hailhq/core/sip_connections.py core/hailhq/core/carrier_routing.py core/tests/test_sip_connections.py
git commit -m "feat(core): connected number helpers: credentials, number shape, limits"
```

## Task A4: Routing by number

**Files:**
- Modify: `core/hailhq/core/carrier_routing.py` (`voice_route` 246-247, `inbound_trunk` 266-267)
- Modify: `core/hailhq/core/telephony_catalog.py` (`_sms_route_exists` 98-101)
- Modify: `api/hailhq/api/routes/calls.py` (line 688), `core/hailhq/core/inbound_calls.py` (lines 148-151)
- Modify: `api/hailhq/api/routes/internal/agent.py` (handover call site, line 1874: `carrier_voice_route(call.provider)` until Task A11)
- Test: `core/tests/test_carrier_routing.py`, `core/tests/test_telephony_catalog.py`, `core/tests/test_inbound_calls.py`, `api/tests/test_calls_api.py`

**Interfaces:**
- Consumes: `PBX`, `connection_of` (Task A3).
- Produces:

```python
async def voice_route(db: AsyncSession, number: PhoneNumber) -> VoiceRoute
# carrier: carrier(number.provider).voice_route()
# pbx: (connection.livekit_outbound_trunk_id, {}); ValueError("this phone system has no address for calls out") when NULL

async def inbound_trunk(db: AsyncSession, number: PhoneNumber) -> str
# carrier: carrier(number.provider).inbound_trunk()
# pbx: connection.livekit_inbound_trunk_id; ValueError when NULL
```

- `_sms_route_exists("pbx")` returns `False`.
- The handover call site (`routes/internal/agent.py:1874`) changes in Task A11; until then it calls a carrier-only wrapper `carrier_voice_route(provider: str) -> VoiceRoute` (today's body), removed in Task A11.

- [ ] **Step 1: Write the failing tests.**

```python
async def test_voice_route_pbx_uses_connection_outbound_trunk(async_session):
    # connection with livekit_outbound_trunk_id="ST_out_pbx"; pbx number on it
    assert await voice_route(async_session, number) == ("ST_out_pbx", {})

async def test_voice_route_pbx_without_host_raises(async_session):
    # connection with pbx_host=None, livekit_outbound_trunk_id=None
    with pytest.raises(ValueError, match="no address for calls out"):
        await voice_route(async_session, number)

async def test_voice_route_carrier_unchanged(async_session, monkeypatch):
    monkeypatch.setattr(settings, "livekit_didww_sip_outbound_trunk_id", "ST_dw")
    assert await voice_route(async_session, didww_number) == ("ST_dw", {})

async def test_inbound_trunk_pbx_uses_connection_trunk(async_session):
    assert await inbound_trunk(async_session, number) == "ST_in_pbx"

def test_pbx_has_no_sms_route():
    assert _sms_route_exists("pbx") is False

# core/tests/test_inbound_calls.py: the trunk check now goes through inbound_trunk(db, number)
async def test_pbx_call_on_other_trunk_is_refused(async_session, active_pbx_number):
    out = await open_inbound_call(async_session, attrs(dialed="+14155550100", trunk_id="ST_in_twilio"))
    assert out == Rejected("carrier_route_failed")
```

  In `api/tests/test_calls_api.py`:

```python
async def test_outbound_call_from_pbx_number_dials_connection_trunk(client, livekit_mock, quote_mock, ...):
    r = await client.post("/v1/calls", json={"from": "+14155550100", "to": "+14155550199", ...})
    assert r.status_code == 201
    kwargs = livekit_mock.create_sip_participant.await_args.kwargs
    assert kwargs["sip_trunk_id"] == "ST_out_pbx"
    assert not kwargs.get("headers")
    assert quote_mock.await_args.args[1][0]["carrier"] == "pbx"
```

- [ ] **Step 2: Run.** `cd core && uv run pytest tests/test_carrier_routing.py tests/test_telephony_catalog.py tests/test_inbound_calls.py -v` and `cd api && uv run pytest tests/test_calls_api.py -v`. Expected: FAIL on the new tests.
- [ ] **Step 3: Implement.** Change both signatures; update `routes/calls.py` to `await voice_route(db, from_number)` at the existing spot (sessions use `expire_on_commit=False`, so `from_number` is still loaded) and `inbound_calls.py` to `await inbound_trunk(db, number)`. Add `carrier_voice_route` and call it at `routes/internal/agent.py:1874`, so the handover still compiles until Task A11.
- [ ] **Step 4: Run** the same commands plus `cd api && uv run pytest tests/test_internal_agent_handover.py -v`. Expected: PASS.
- [ ] **Step 5: Commit.**

```bash
git add core/hailhq/core/carrier_routing.py core/hailhq/core/telephony_catalog.py \
  core/hailhq/core/inbound_calls.py api/hailhq/api/routes/calls.py \
  api/hailhq/api/routes/internal/agent.py \
  core/tests/test_carrier_routing.py core/tests/test_telephony_catalog.py \
  core/tests/test_inbound_calls.py api/tests/test_calls_api.py
git commit -m "refactor(routing): pick voice and inbound trunks by number, not carrier"
```

## Task A5: Agent routing, default caller ID and the unverified refusal

**Files:**
- Modify: `core/hailhq/core/inbound_routing.py` (`register` 37-67, `unregister` 70-97)
- Modify: `api/hailhq/api/numbers.py` (`resolve_org_number` 28-52)
- Modify: `api/hailhq/api/routes/numbers.py` (`_reject_if_released` 214-222)
- Test: `core/tests/test_inbound_routing.py`, `api/tests/test_numbers_api.py`, `api/tests/test_calls_api.py`

**Interfaces:**
- Consumes: `inbound_trunk(db, number)` (Task A4), `LiveKitClient.add_inbound_number`.
- `register` for `pbx`: no carrier attach; `await lk.add_inbound_number(trunk, e164)` (a set-add, so a re-add is harmless); stamp `inbound_registered_at`.
- `unregister` for `pbx`: clear `inbound_registered_at` only. The number stays on the trunk while the row is `unverified` or `active` (spec section 2); a call then gets a `no_agent` refusal the customer can see.
- `resolve_org_number`, only when `explicit_e164 is None`: outer join `sip_connections` and skip `provider = 'pbx'` rows whose connection has `livekit_outbound_trunk_id IS NULL`.
- `route_number` already refuses every non-`active` row through `_reject_if_released` (`api/hailhq/api/routes/numbers.py:214`, 422, `loc=["path", "number_id"]`). Give `unverified` its own detail there: `"this number is not checked yet; run the ownership check first"`. The same helper guards `enable_sms`.

- [ ] **Step 1: Write the failing tests.**

```python
async def test_register_pbx_skips_carrier_and_adds_to_connection_trunk(async_session, hooks):
    await inbound_routing.register(async_session, lk, pbx_number)
    hooks["attach"].assert_not_awaited()
    lk.add_inbound_number.assert_awaited_once_with("ST_in_pbx", "+14155550100")
    assert pbx_number.inbound_registered_at is not None

async def test_unregister_pbx_keeps_number_on_trunk(async_session, hooks):
    await inbound_routing.unregister(async_session, lk, pbx_number)
    lk.remove_inbound_number.assert_not_awaited()
    assert pbx_number.inbound_registered_at is None

async def test_route_number_refuses_unverified(client, ...):
    r = await client.patch(f"/v1/numbers/{unverified.id}", json={"voice_agent_id": str(agent.id)})
    assert r.status_code == 422
    assert "not checked yet" in r.text

async def test_default_caller_id_skips_answer_only_pbx_number(...):
    # org: older pbx number on a connection with no pbx_host, newer twilio number
    assert (await resolve_org_number(db, org, None)).provider == "twilio"

async def test_default_caller_id_may_be_pbx_with_host(...):
    # org: older pbx number on a connection with livekit_outbound_trunk_id="ST_out_pbx", newer twilio number
    assert (await resolve_org_number(db, org, None)).provider == "pbx"
```

- [ ] **Step 2: Run.** `cd core && uv run pytest tests/test_inbound_routing.py -v`; `cd api && uv run pytest tests/test_numbers_api.py tests/test_calls_api.py -v`. Expected: FAIL on the new tests.
- [ ] **Step 3: Implement** the branches above.
- [ ] **Step 4: Run.** Same commands. Expected: PASS.
- [ ] **Step 5: Commit.**

```bash
git add core/hailhq/core/inbound_routing.py api/hailhq/api/numbers.py api/hailhq/api/routes/numbers.py \
  core/tests/test_inbound_routing.py api/tests/test_numbers_api.py api/tests/test_calls_api.py
git commit -m "feat(numbers): route connected numbers on their own trunk; no unchecked routing"
```

## Task A6: Connection service (create, update, rotate, delete)

**Files:**
- Create: `api/hailhq/api/connected_numbers.py`
- Test: `api/tests/test_connected_numbers.py`

**Interfaces:**
- Consumes: Task A2 `LiveKitClient` trunk methods, Task A3 helpers, `org_lock` (`hailhq.core.db`).
- Produces:

```python
class ConnectedNumberError(Exception):
    code: str      # one of the codes below
    field: str | None
# codes: "invalid_input", "number_taken", "invalid_number", "has_numbers",
#        "connection_deleted", "not_unverified", "check_in_progress", "check_limit",
#        "no_funds", "not_callable", "pool_exhausted", "livekit_failed"

UNSET: Final = object()

async def create_connection(
    db: AsyncSession, lk: LiveKitClient, *, organization_id: UUID, label: str,
    default_region: str, pbx_host: str | None = None, pbx_port: int = 5060,
    pbx_transport: str = "auto", pbx_username: str | None = None,
    pbx_password: str | None = None,
) -> tuple[SipConnection, str]   # (row, inbound password in plain text, shown once)

async def update_connection(
    db: AsyncSession, lk: LiveKitClient, connection: SipConnection, *,
    label=UNSET, pbx_host=UNSET, pbx_port=UNSET, pbx_transport=UNSET,
    pbx_username=UNSET, pbx_password=UNSET,
) -> SipConnection

async def rotate_inbound_password(
    db: AsyncSession, lk: LiveKitClient, connection: SipConnection
) -> str   # new plain-text password

async def delete_connection(
    db: AsyncSession, lk: LiveKitClient, connection: SipConnection
) -> None  # "has_numbers" while any row on it is not released or failed
```

- Validation in `create_connection` and `update_connection` (all `invalid_input` with `field`): label 1 to 80 characters after strip; `default_region` in `phonenumbers.SUPPORTED_REGIONS`; `pbx_transport` in the four values; `pbx_port` 1 to 65535; `pbx_host` a bare host name or IP, at most 253 characters, no scheme, no port, no path; `pbx_password` set only with `pbx_username`.
- `create_connection` order: check the cipher key (raises `SecretKeyMissing` before any LiveKit call); new `uuid4()` id; create the inbound trunk; create the outbound trunk when `pbx_host`; insert the row; commit. When the insert or commit fails, delete the trunks it created (best effort, logged) and re-raise.
- `update_connection` on `pbx_host`: NULL to a host creates the outbound trunk; a host to NULL deletes it and sets `livekit_outbound_trunk_id = NULL`; a host to a host updates the trunk.
- `delete_connection`: delete both trunks (404 counts as done), set `deleted_at`, commit. The row stays (tombstoned numbers still point at it, `ON DELETE RESTRICT`).

- [ ] **Step 1: Write the failing tests** (`lk = AsyncMock(spec=LiveKitClient)` returning `"ST_in"` / `"ST_out"`; `HAIL_PROVIDER_SECRET_KEY` set with `generate_key()`):

```python
async def test_create_connection_makes_both_trunks_and_stores_secrets(async_session, lk):
    conn, password = await create_connection(async_session, lk, organization_id=ORG,
        label="Main office", default_region="US", pbx_host="pbx.example.com")
    assert len(password) >= 24
    assert conn.livekit_inbound_trunk_id == "ST_in"
    assert conn.livekit_outbound_trunk_id == "ST_out"
    assert decrypt_secret(conn.encrypted_inbound_password) == password
    lk.create_pbx_inbound_trunk.assert_awaited_once_with(conn.id, conn.inbound_username, password)

async def test_create_connection_answer_only_has_no_outbound_trunk(async_session, lk):
    conn, _ = await create_connection(async_session, lk, organization_id=ORG, label="Main office", default_region="GB")
    assert conn.livekit_outbound_trunk_id is None
    lk.create_pbx_outbound_trunk.assert_not_awaited()

@pytest.mark.parametrize("field,kwargs", [
    ("label", {"label": " "}), ("label", {"label": "x" * 81}),
    ("default_region", {"default_region": "XX"}),
    ("pbx_host", {"pbx_host": "https://pbx.example.com"}),
    ("pbx_host", {"pbx_host": "pbx.example.com:5060"}),
    ("pbx_transport", {"pbx_transport": "ws"}),
    ("pbx_password", {"pbx_password": "only-password"}),
])
async def test_create_connection_rejects_bad_input(async_session, lk, field, kwargs):
    args = {"organization_id": ORG, "label": "Main office", "default_region": "US", **kwargs}
    with pytest.raises(ConnectedNumberError) as e:
        await create_connection(async_session, lk, **args)
    assert (e.value.code, e.value.field) == ("invalid_input", field)
    lk.create_pbx_inbound_trunk.assert_not_awaited()

async def test_create_connection_deletes_trunks_when_insert_fails(async_session, lk, monkeypatch):
    monkeypatch.setattr(async_session, "flush", AsyncMock(side_effect=IntegrityError("x", {}, None)))
    with pytest.raises(IntegrityError):
        await create_connection(async_session, lk, organization_id=ORG, label="Main office",
                                default_region="US", pbx_host="pbx.example.com")
    assert {c.args[0] for c in lk.delete_sip_trunk.await_args_list} == {"ST_in", "ST_out"}

async def test_update_connection_adds_and_drops_outbound_trunk(async_session, lk, answer_only_conn):
    conn = await update_connection(async_session, lk, answer_only_conn, pbx_host="pbx.example.com")
    assert conn.livekit_outbound_trunk_id == "ST_out"
    conn = await update_connection(async_session, lk, conn, pbx_host=None)
    assert conn.livekit_outbound_trunk_id is None
    lk.delete_sip_trunk.assert_awaited_once_with("ST_out")

async def test_update_connection_host_to_host_updates_trunk(async_session, lk, conn):
    lk.reset_mock()
    await update_connection(async_session, lk, conn, pbx_host="pbx2.example.com")
    assert lk.update_pbx_outbound_trunk.await_args.args[:2] == ("ST_out", "pbx2.example.com")
    lk.create_pbx_outbound_trunk.assert_not_awaited()

async def test_rotate_inbound_password_updates_trunk_and_row(async_session, lk, conn, old_password):
    new = await rotate_inbound_password(async_session, lk, conn)
    lk.set_pbx_inbound_credentials.assert_awaited_once_with("ST_in", conn.inbound_username, new)
    assert decrypt_secret(conn.encrypted_inbound_password) == new
    assert new != old_password

async def test_delete_connection_refuses_while_numbers_live(async_session, lk, conn, unverified_number):
    with pytest.raises(ConnectedNumberError) as e:
        await delete_connection(async_session, lk, conn)
    assert e.value.code == "has_numbers"
    lk.delete_sip_trunk.assert_not_awaited()

async def test_delete_connection_deletes_trunks_and_tombstones(async_session, lk, conn, released_number):
    await delete_connection(async_session, lk, conn)
    assert conn.deleted_at is not None
    assert {c.args[0] for c in lk.delete_sip_trunk.await_args_list} == {"ST_in", "ST_out"}
```

- [ ] **Step 2: Run.** `cd api && uv run pytest tests/test_connected_numbers.py -v`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** the module.
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit.**

```bash
git add api/hailhq/api/connected_numbers.py api/tests/test_connected_numbers.py
git commit -m "feat(api): connection service with per-connection LiveKit trunks"
```

## Task A7: Add and remove a connected number, release branch, 7-day cleanup

**Files:**
- Modify: `api/hailhq/api/connected_numbers.py`
- Modify: `api/hailhq/api/routes/numbers.py` (`release_org_number` 247-352, before the `CARRIERS` check at 286)
- Modify: `api/hailhq/api/main.py` (`_order_reconciler_loop` 146-163)
- Test: `api/tests/test_connected_numbers.py`, `api/tests/test_numbers_api.py`

**Interfaces:**
- Produces:

```python
async def add_connected_number(
    db: AsyncSession, lk: LiveKitClient, connection: SipConnection, e164: str
) -> PhoneNumber   # state "unverified", already on the inbound trunk

async def remove_connected_number(
    db: AsyncSession, lk: LiveKitClient, number: PhoneNumber
) -> PhoneNumber   # state "released", released_at set; idempotent

async def remove_stale_unverified_numbers(
    lk: LiveKitClient | None, *, now: datetime | None = None
) -> int           # rows removed; 0 and a warning when lk is None
```

- `add_connected_number`: `invalid_number` unless `phonenumbers.is_valid_number`; `connection_deleted` when `deleted_at` is set; `org_lock`; insert with `number_shape(e164)`; an `IntegrityError` on `phone_numbers_e164_live_uniq` rolls back and raises `number_taken`; `lk.add_inbound_number(connection.livekit_inbound_trunk_id, e164)` before the commit (failure: rollback, `livekit_failed`); if the commit then fails, remove it from the trunk (best effort) and re-raise.
- `remove_connected_number`: `org_lock`, refresh; already `released` returns as is; `lk.remove_inbound_number(trunk, e164)` (failure: `livekit_failed`, row unchanged); then `released`, `released_at`, `voice_agent_id = None`, `sms_agent_id = None`, `inbound_registered_at = None`; commit.
- `release_org_number`: a `pbx` row returns `await remove_connected_number(db, lk, number)`. `lk is None` -> 503 `"inbound calls are not configured on this server"`. `livekit_failed` -> 502 `"could not take the number off the phone system trunk"`.
- Cleanup: rows with `provider = 'pbx'`, `provisioning_state = 'unverified'`, `created_at < now - UNVERIFIED_TTL`; each removed in its own transaction; one failure is logged and the rest continue. Called once per `_order_reconciler_loop` pass with `await get_livekit_optional()`.

- [ ] **Step 1: Write the failing tests.**

```python
async def test_add_number_is_unverified_voice_only_and_on_trunk(async_session, lk, conn):
    n = await add_connected_number(async_session, lk, conn, "+14155550100")
    assert (n.provider, n.provisioning_state, n.capabilities, n.is_pool) == ("pbx", "unverified", ["voice"], False)
    assert (n.country_code, n.number_type) == ("US", "local")
    lk.add_inbound_number.assert_awaited_once_with(conn.livekit_inbound_trunk_id, "+14155550100")

async def test_add_number_taken_elsewhere_is_a_clean_error(async_session, lk, conn):
    # a live twilio row for +14155550100 in another org
    with pytest.raises(ConnectedNumberError) as e:
        await add_connected_number(async_session, lk, conn, "+14155550100")
    assert e.value.code == "number_taken"
    lk.add_inbound_number.assert_not_awaited()

async def test_add_number_rejects_invalid(async_session, lk, conn):
    with pytest.raises(ConnectedNumberError) as e:
        await add_connected_number(async_session, lk, conn, "+1415")
    assert e.value.code == "invalid_number"
    assert await count_rows(async_session, PhoneNumber, e164="+1415") == 0
    lk.add_inbound_number.assert_not_awaited()

async def test_remove_last_number_keeps_placeholder(async_session, lk, conn):
    n = await add_connected_number(async_session, lk, conn, "+14155550100")
    await remove_connected_number(async_session, lk, n)
    lk.remove_inbound_number.assert_awaited_once_with(conn.livekit_inbound_trunk_id, "+14155550100")
    assert n.provisioning_state == "released"
    assert n.released_at is not None

async def test_removed_number_can_be_added_again(async_session, lk, conn):
    first = await add_connected_number(async_session, lk, conn, "+14155550100")
    await remove_connected_number(async_session, lk, first)
    second = await add_connected_number(async_session, lk, conn, "+14155550100")
    assert second.id != first.id
    assert second.provisioning_state == "unverified"
    assert first.provisioning_state == "released"

async def test_stale_unverified_numbers_are_removed_after_7_days(async_session, lk, conn):
    old = await seed_pbx(conn, "+14155550100", state="unverified", created_at=NOW - timedelta(days=7, minutes=1))
    young = await seed_pbx(conn, "+14155550101", state="unverified", created_at=NOW - timedelta(days=6))
    active = await seed_pbx(conn, "+14155550102", state="active", created_at=NOW - timedelta(days=30))
    assert await remove_stale_unverified_numbers(lk, now=NOW) == 1
    assert [await state_of(n) for n in (old, young, active)] == ["released", "unverified", "active"]

async def test_stale_cleanup_without_livekit_does_nothing(async_session, conn):
    await seed_pbx(conn, "+14155550100", state="unverified", created_at=NOW - timedelta(days=8))
    assert await remove_stale_unverified_numbers(None, now=NOW) == 0

async def test_release_endpoint_removes_pbx_number(client, livekit_mock, pbx_number, ...):
    r = await client.delete(f"/v1/numbers/{pbx_number.id}")
    assert r.status_code == 204
    assert await state_of(pbx_number) == "released"
    livekit_mock.remove_inbound_number.assert_awaited_once_with("ST_in_pbx", pbx_number.e164)

async def test_number_reads_accept_connected_rows(client, unverified, active_pbx, ...):
    assert (await client.get("/v1/numbers")).status_code == 200
    assert (await client.get(f"/v1/numbers/{unverified.id}")).json()["provisioning_state"] == "unverified"

async def test_enable_sms_on_connected_number_is_refused(client, ...):
    r = await client.post(f"/v1/numbers/{active_pbx.id}/enable-sms")
    assert r.status_code == 422   # no sms capability; never a 500
```

  Test helpers in the file: `seed_pbx(conn, e164, state, created_at)` inserts a `pbx` row; `state_of(n)` re-reads `provisioning_state`; `count_rows(db, Model, **where)`.

- [ ] **Step 2: Run.** `cd api && uv run pytest tests/test_connected_numbers.py tests/test_numbers_api.py -v`. Expected: FAIL on the new tests.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Same command. Expected: PASS.
- [ ] **Step 5: Commit.**

```bash
git add api/hailhq/api/connected_numbers.py api/hailhq/api/routes/numbers.py api/hailhq/api/main.py \
  api/tests/test_connected_numbers.py api/tests/test_numbers_api.py
git commit -m "feat(api): add, remove and expire connected numbers"
```

## Task A8: Inbound lookup and caller number

**Files:**
- Modify: `core/hailhq/core/sip_connections.py`
- Modify: `core/hailhq/core/inbound_calls.py` (`open_inbound_call` 137-178)
- Modify: `voicebot/hailhq/voicebot/agent.py` (`open_inbound_from_room`, `SipAttributes(...)` at 1558-1566)
- Test: `core/tests/test_sip_connections.py`, `core/tests/test_inbound_calls.py`, `voicebot/tests/test_agent_inbound.py`

**Interfaces:**
- Produces:

```python
def carrier_caller(raw: str) -> str
# today's voicebot _e164 rule, moved here: digits only -> "+" + digits; else unchanged

def normalize_caller(raw: str, default_region: str) -> str
# "" or not digits/+ -> raw; 1-6 digits -> raw (an extension);
# else phonenumbers.parse(raw, default_region): possible -> E.164, else raw
```

- One copy of each rule. The voicebot deletes `_e164` and imports `carrier_caller` from `hailhq.core.sip_connections` for `dialed`.
- `SipAttributes.caller` now carries the raw `sip.phoneNumber` (stripped).
- `open_inbound_call`: after the number is found, `caller = normalize_caller(raw, connection.default_region)` for `pbx`, else `carrier_caller(raw)`; every row, event and metadata uses that value. The trunk check is Task A4's (`inbound_trunk(db, number)`); this task adds none.

- [ ] **Step 1: Write the failing tests.**

```python
@pytest.mark.parametrize("raw,region,out", [
    ("101", "US", "101"),
    ("123456", "US", "123456"),
    ("4155550100", "US", "+14155550100"),
    ("+14155550100", "GB", "+14155550100"),
    ("02079460000", "GB", "+442079460000"),
    ("anonymous", "US", "anonymous"),
    ("", "US", ""),
])
def test_normalize_caller_keeps_extension(raw, region, out):
    assert normalize_caller(raw, region) == out

def test_carrier_caller_matches_old_rule():
    assert carrier_caller("442079460000") == "+442079460000"
    assert carrier_caller("anonymous") == "anonymous"

async def test_pbx_call_answers_on_connection_trunk_with_normalized_caller(async_session, active_pbx_number):
    out = await open_inbound_call(async_session, attrs(dialed="+14155550100", caller="4155550199", trunk_id="ST_in_pbx"))
    assert isinstance(out, Accepted)
    assert out.metadata["from_e164"] == "+14155550199"
    assert (await async_session.get(Call, out.call_id)).from_e164 == "+14155550199"

async def test_pbx_call_from_extension_keeps_digits(async_session, active_pbx_number):
    out = await open_inbound_call(async_session, attrs(dialed="+14155550100", caller="101", trunk_id="ST_in_pbx"))
    assert out.metadata["from_e164"] == "101"

async def test_carrier_call_caller_still_gets_plus(async_session, twilio_number):
    out = await open_inbound_call(async_session, attrs(dialed=twilio_number.e164, caller="14155550199", trunk_id="ST_in_twilio"))
    assert out.metadata["from_e164"] == "+14155550199"
```

  Voicebot, in `test_agent_inbound.py`:

```python
def test_sip_attributes_pass_raw_caller_and_plus_dialed():
    attrs = sip_attributes_of(fake_participant({"sip.phoneNumber": " 101 ", "sip.trunkPhoneNumber": "14155550100"}))
    assert attrs.caller == "101"
    assert attrs.dialed == "+14155550100"
```

  `sip_attributes_of` is the name the builder gives the existing `SipAttributes(...)` construction (lines 1558-1566) once it is a small function; keep the existing name if one exists.

- [ ] **Step 2: Run.** `cd core && uv run pytest tests/test_sip_connections.py tests/test_inbound_calls.py -v`; `cd voicebot && uv run pytest tests/test_agent_inbound.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Same commands. Expected: PASS.
- [ ] **Step 5: Commit.**

```bash
git add core/hailhq/core/sip_connections.py core/hailhq/core/inbound_calls.py \
  voicebot/hailhq/voicebot/agent.py core/tests/test_sip_connections.py \
  core/tests/test_inbound_calls.py voicebot/tests/test_agent_inbound.py
git commit -m "feat(inbound): answer connected numbers on their trunk; keep extension callers as dialed"
```

## Task A9: Zero-cost legs for connected calls

**Files:**
- Modify: `core/hailhq/core/call_legs.py` (`record_call_legs` 61-99)
- Modify: `docs/operations/call-leg-costs.md` (two lines: `pbx` legs are written `found` at 0; hail-website charges a `pbx` handover leg from its `answered_at` and `left_at`)
- Test: `core/tests/test_call_legs.py`

**Interfaces:**
- `record_call_legs`: when the call's provider is `PBX`, each row gets `cost_status = "found"`, `cost_usd = Decimal(0)`, `usd_per_minute = Decimal(0)`, `cost_checked_at = now`.
- Handover price input for Part B: no new column. The voicebot's `LegTracker` (`voicebot/hailhq/voicebot/leg_tracker.py`) already records every SIP leg with `role`, `answered_at` (first `sip.callStatus == "active"`) and `left_at` (or the end time when the leg is still in the room), for any trunk. The call's shutdown writes the legs before `on_call_end` writes the usage event (`voicebot/hailhq/voicebot/agent.py`, the `record_call_legs` call before `on_call_end`), so the rater sees the handover leg when it prices the call. The `pbx` branch must keep `role`, `answered_at` and `left_at` as given.

- [ ] **Step 1: Write the failing test.**

```python
async def test_pbx_legs_are_found_at_zero(async_session, pbx_call):
    leg_call = LegRecord(role="call", direction="inbound", sip_call_id="SCL_1", carrier_call_id=None,
                         to_e164="+14155550100", joined_at=T0, answered_at=T0, left_at=T0 + timedelta(seconds=180))
    leg_handover = LegRecord(role="handover", direction="outbound", sip_call_id="SCL_2", carrier_call_id=None,
                             to_e164="800", joined_at=T0 + timedelta(seconds=60),
                             answered_at=T0 + timedelta(seconds=70), left_at=T0 + timedelta(seconds=130))
    await record_call_legs(pbx_call.id, [leg_call, leg_handover])
    rows = {r.role: r for r in await legs_of(async_session, pbx_call.id)}
    assert {(r.provider, r.cost_status, r.cost_usd) for r in rows.values()} == {("pbx", "found", Decimal("0"))}
    assert rows["handover"].answered_at == T0 + timedelta(seconds=70)
    assert rows["handover"].left_at == T0 + timedelta(seconds=130)
    assert rows["handover"].to_e164 == "800"
    assert await fetch_leg_costs(now=T0 + timedelta(hours=1)) == 0   # never asked of a carrier
```

- [ ] **Step 2: Run.** `cd core && uv run pytest tests/test_call_legs.py -v`. Expected: FAIL on the new test (`pending` or `missing`).
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `cd core && uv run pytest tests/test_call_legs.py -v`. Expected: PASS.
- [ ] **Step 5: Commit.**

```bash
git add core/hailhq/core/call_legs.py core/tests/test_call_legs.py docs/operations/call-leg-costs.md
git commit -m "feat(call-legs): connected calls cost Hail nothing per leg"
```

## Task A10: Extension contacts in handover lists

**Files:**
- Modify: `core/hailhq/core/handover.py` (`HandoverPerson` 140-149, `resolve_people` 220-246, `validate_handover` 249-286, `load_handover` 368-418, `handover_targets` 421-441)
- Modify: `api/hailhq/api/routes/calls.py` and `core/hailhq/core/inbound_calls.py` (the `handover_targets` calls in the dispatch metadata)
- Test: `core/tests/test_handover.py`

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True)
class HandoverPerson:
    contact_id: str
    name: str
    phone_e164: str | None
    email: str | None = None
    extension: str | None = None

async def handover_targets(
    db: AsyncSession, agent_id: UUID | None, on_call_e164: str | None = None,
    *, via_pbx: bool = False,
) -> list[dict]
```

- `load_handover` rows gain `"extension"` (`None` for members).
- `validate_handover`: a person with neither phone nor extension -> `HandoverInvalid(f"{name} has no phone number or extension")`. An extension-only person skips the country and gate checks. A person with a phone keeps every check.
- `handover_targets`: a row is offered when it has a phone, or when `via_pbx` and it has an extension. `on_call` is set when the phone equals `on_call_e164`, or when `via_pbx` and the extension equals it.
- `routes/calls.py` passes `via_pbx=from_number.provider == PBX`; `inbound_calls.py` passes `via_pbx=number.provider == PBX`.

- [ ] **Step 1: Write the failing tests.**

```python
async def test_extension_contact_offered_only_on_pbx_call(async_session):
    # agent links Contact(name="Support line", extension="800")
    assert await handover_targets(async_session, agent.id) == []
    assert [t["label"] for t in await handover_targets(async_session, agent.id, via_pbx=True)] == ["Support line"]

async def test_extension_on_the_line_is_flagged(async_session):
    t = await handover_targets(async_session, agent.id, "800", via_pbx=True)
    assert t[0]["on_call"] is True

async def test_validate_accepts_extension_only_contact(async_session, monkeypatch):
    gate = AsyncMock(side_effect=AssertionError("gate must not run"))
    monkeypatch.setattr(handover, "check_handover_allowed", gate)
    assert await validate_handover(async_session, ORG, items_for(support_line_800)) is None
    gate.assert_not_awaited()

async def test_validate_still_checks_phone_of_contact_with_extension(async_session):
    # contact: extension "800", phone in a country Hail does not sell numbers in
    with pytest.raises(HandoverInvalid, match="country Hail does not call"):
        await validate_handover(async_session, ORG, items_for(contact_with_ext_and_unsold_phone))

async def test_validate_refuses_contact_without_phone_or_extension(async_session):
    with pytest.raises(HandoverInvalid, match="has no phone number or extension$"):
        await validate_handover(async_session, ORG, items_for(email_only_contact))
```

  `items_for(contact)` returns `[HandoverItem(contact_id=<the contact's wire id>)]`; the three contacts are `Contact` rows made in the test.

- [ ] **Step 2: Run.** `cd core && uv run pytest tests/test_handover.py -v`. Expected: FAIL on the new tests.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same file and `cd api && uv run pytest tests/test_agents_api.py -v`. Expected: PASS.
- [ ] **Step 5: Commit.**

```bash
git add core/hailhq/core/handover.py api/hailhq/api/routes/calls.py core/hailhq/core/inbound_calls.py \
  core/tests/test_handover.py
git commit -m "feat(handover): extension contacts on connected calls"
```

## Task A11: Handover through the phone system

**Files:**
- Modify: `api/hailhq/api/routes/internal/agent.py` (`_linked_person` 1795-1810, `agent_handover` 1834-1893)
- Modify: `core/hailhq/core/carrier_routing.py` (remove `carrier_voice_route` from Task A4)
- Test: `api/tests/test_internal_agent_handover.py`

**Interfaces:**
- Consumes: `voice_route(db, number)` (Task A4), `HandoverPerson.extension` (Task A10).
- `agent_handover` rules, in order, for a call with `provider == PBX` (`via_pbx`):
  1. Hail number row: `call.to_number_id` (inbound) or `call.from_number_id` (outbound); missing -> deny `carrier_route_failed`.
  2. Target: on a `pbx` call the extension wins: a contact with both an extension and a phone is dialed at its extension, and the phone checks of rule 4 do not run. A contact with only a phone is dialed at the phone. Neither -> deny.
  3. Target equal to `_other_party(call)` -> `contact_is_on_call` (same spoken line as today).
  4. A phone target keeps the sold-country check and `check_handover_allowed`. An extension skips both.
  5. `trunk_id, headers = await voice_route(db, number)`; `ValueError` -> deny `carrier_route_failed`.
  6. `from_e164`: `_other_party(call)` when it is `+E.164` or a 1-6 digit extension (`^\+[1-9]\d{1,14}$` or `^\d{1,6}$`); anything else (`anonymous`, empty, a SIP URI) -> the Hail number. Spec section 4 keeps an anonymous caller as given on the call row; only the handover caller ID changes. If the Task A2 probe fell back, the Hail number always.
  7. `_handover_cap(db, call, target, hail_number)` unchanged. It already quotes `[main]` and `[main, second]`, both with `carrier: call.provider`, so on a `pbx` call hail-website prices the second leg at the handover price (Task B3).
- Carrier calls: extension-only contacts are denied (`deny`); everything else is today's behavior, now through `voice_route(db, number)`.
- `AgentHandoverResponse.to_e164` carries the extension as is (internal field, name unchanged).

- [ ] **Step 1: Write the failing tests.**

```python
async def test_handover_to_extension_on_pbx_call(client, ...):
    # inbound pbx call from +14155550199; contact "Support line" ext "800"
    body = (await post_handover(...)).json()
    assert body["ok"] is True
    assert body["to_e164"] == "800"
    assert body["from_e164"] == "+14155550199"   # the original caller
    assert body["trunk_id"] == "ST_out_pbx"

@pytest.mark.parametrize("allowed", [False, True])
async def test_handover_to_phone_on_pbx_call_keeps_gate(client, gate_mock, allowed, ...):
    # inbound pbx call from +14155550199; contact with phone +14155550123 only
    gate_mock.return_value = GateResult(allowed=allowed)
    body = (await post_handover(...)).json()
    assert body["ok"] is allowed
    if allowed:
        assert body["from_e164"] == "+14155550199"
        assert body["trunk_id"] == "ST_out_pbx"

async def test_handover_to_contact_with_phone_and_extension_dials_extension(client, gate_mock, ...):
    # pbx call; contact ext "800" and phone +14155550123
    body = (await post_handover(...)).json()
    assert body["to_e164"] == "800"
    gate_mock.assert_not_awaited()

async def test_handover_refuses_extension_on_the_line(client, ...):
    # inbound pbx call from "101"; contact "Support line" ext "101"
    body = (await post_handover(...)).json()
    assert body["ok"] is False
    assert body["spoken"] == ON_CALL_LINE.format(name="Support line")

async def test_handover_from_extension_caller_keeps_extension(client, ...):
    # inbound pbx call from "101"; contact ext "800"
    assert (await post_handover(...)).json()["from_e164"] == "101"

async def test_handover_from_anonymous_caller_uses_hail_number(client, ...):
    # inbound pbx call from "anonymous" to +14155550100; contact ext "800"
    assert (await post_handover(...)).json()["from_e164"] == "+14155550100"

async def test_handover_without_pbx_host_is_denied(client, ...):
    # connection with pbx_host=None, livekit_outbound_trunk_id=None
    body = (await post_handover(...)).json()
    assert body["ok"] is False
    assert (await last_handover_event(call)).payload["reason"] == "carrier_route_failed"

async def test_extension_contact_denied_on_carrier_call(client, ...):
    # twilio call, extension-only contact "800"
    body = (await post_handover(...)).json()
    assert body["ok"] is False
    assert body.get("trunk_id") is None

async def test_handover_quote_on_pbx_call_sends_two_pbx_legs(client, quote_mock, ...):
    # inbound pbx call from +14155550199 to +14155550100; contact ext "800"
    await post_handover(...)
    legs = max((c.args[1] for c in quote_mock.await_args_list), key=len)
    assert [(l["direction"], l["carrier"], l["number_e164"]) for l in legs] == [
        ("inbound", "pbx", "+14155550100"), ("outbound", "pbx", "800")]

# Passes before and after this task (agent.py:2102 already skips a contact
# with no phone). Kept as a regression test.
async def test_handover_notice_to_extension_contact_skips_text(client, ...):
    r = await client.post("/internal/agent/handover-sms", json={...})   # extension-only contact
    assert r.json() == {"sms": "skipped:no_contact"}
```

- [ ] **Step 2: Run.** `cd api && uv run pytest tests/test_internal_agent_handover.py -v`. Expected: FAIL on the new tests, except `test_handover_notice_to_extension_contact_skips_text` (passes before and after).
- [ ] **Step 3: Implement** the rules; delete `carrier_voice_route`.
- [ ] **Step 4: Run** the same file and `cd core && uv run pytest tests/test_carrier_routing.py -v`. Expected: PASS.
- [ ] **Step 5: Commit.**

```bash
git add api/hailhq/api/routes/internal/agent.py core/hailhq/core/carrier_routing.py \
  api/tests/test_internal_agent_handover.py
git commit -m "feat(handover): dial extensions and numbers through the phone system with the caller's ID"
```

## Task A12: Ownership check, inbound side

**Files:**
- Modify: `core/hailhq/core/sip_connections.py` (code hash, code match, check-in-flight helper)
- Modify: `core/hailhq/core/inbound_calls.py` (new result type; branch where `active_dedicated_number` returns `None`, line 140)
- Modify: `voicebot/hailhq/voicebot/agent.py` (`open_inbound_from_room` 1535-1580)
- Test: `core/tests/test_sip_connections.py`, `core/tests/test_inbound_calls.py`, `voicebot/tests/test_agent_inbound.py`

**Interfaces:**
- Consumes: `CHECK_CALL_META`, `CHECK_EVENT_KIND`, `CHECK_WINDOW`, `CHECK_CODE_SECONDS` (Task A3).
- Produces in `sip_connections.py` (one copy, used by this task and Task A13):

```python
CHECK_CODE_META = "pbx_check_code_sha256"   # calls.metadata key on the check call
CHECK_CODE_DIGITS = 6

def hash_check_code(code: str) -> str   # sha256 hex digest
def check_code_matches(digits: str, code_sha256: str) -> bool
# True only when len(digits) == CHECK_CODE_DIGITS and hmac.compare_digest(hash_check_code(digits), code_sha256)

async def check_in_flight(db: AsyncSession, number_id: UUID, now: datetime) -> Call | None
# newest Call with metadata_[CHECK_CALL_META] == str(number_id), status not in
# TERMINAL_CALL_STATUSES, requested_at >= now - CHECK_WINDOW
```

- Produces in `inbound_calls.py`:

```python
@dataclass(frozen=True)
class CheckArrived:
    """An ownership check call reached the connection's trunk. No Call row
    for this leg; the check call row (outbound) carries the result. The
    voicebot still has to receive the code before the number turns active."""
    number_id: UUID
    check_call_id: UUID

async def open_inbound_call(db, attrs) -> Accepted | Rejected | CheckArrived

async def finish_check(
    db: AsyncSession, arrived: CheckArrived, digits: str, *, now: datetime | None = None
) -> bool
# check_code_matches(digits, check_call.metadata_[CHECK_CODE_META]):
#   True  -> number active, acquired_at = now, CallEvent {"outcome": "passed"}
#   False -> CallEvent {"outcome": "code_not_received"}, number stays unverified
# commits; returns the match
```

- Produces in the voicebot:

```python
# spoken_lines.py: SpokenLines.pbx_check, "Your phone system is now connected
# to Hail. Goodbye." in every supported language
async def collect_check_digits(room: rtc.Room, seconds: float = CHECK_CODE_SECONDS) -> str
# listens to "sip_dtmf_received"; returns at the 6th digit (the first 6 only),
# or after `seconds` with the digits it has
async def play_check_line(ctx: JobContext, language: str | None = None) -> None  # best effort, at most 10 s
```

- Arrival rule, run only when no `active` row matches the dialed number:
  1. A `pbx` row with that `e164` in state `unverified`, else `Rejected("unknown_number")`.
  2. `check_in_flight(db, number.id, now)`. None -> `Rejected("unknown_number")`.
  3. `attrs.trunk_id == connection.livekit_inbound_trunk_id`: add `CallEvent(call_id=check.id, kind=CHECK_EVENT_KIND, payload={"outcome": "arrived"})`; commit; return `CheckArrived(number.id, check.id)`. The row stays `unverified`: arrival alone proves nothing (the customer's own trunk can send an INVITE for any number).
  4. Any other trunk: add `CallEvent(..., payload={"outcome": "not_authenticated"})`; commit; return `Rejected("pbx_check_wrong_trunk")`. The row stays `unverified`.
- **Code loopback (closes the "own INVITE also passes" hole).** The check call carries a 6-digit code that only travels over the real phone network. Task A13 makes the code, stores only `hash_check_code(code)` in the check Call's `metadata_[CHECK_CODE_META]`, and its voicebot sender sends it as DTMF after the answer.
  - Voicebot on `CheckArrived`: `digits = await collect_check_digits(ctx.room)`; `passed = await finish_check(db, arrived, digits)`; when `passed`, `play_check_line(ctx)`.
  - Only the first 6 digits received count. There is no search over a longer string, so 15 s of key presses cannot try many codes. A digit typed before the code makes the check fail; the customer runs it again (5 a day).
  - Then, also on any error, `ctx.delete_room()` and `ctx.shutdown(reason="pbx_check")`; `open_inbound_from_room` returns `None`.
- `play_check_line(ctx, language)` builds `AgentSession(tts=build_tts(language=language))` (Hail's house voice, no STT or LLM), says `spoken_lines(language).pbx_check` (English when unknown) with `allow_interruptions=False` inside `asyncio.timeout(10)`; any error is logged and ignored.
- Risk: a phone system that drops DTMF fails the check with `code_not_received`. The setup page says to keep RFC 2833 / RFC 4733 DTMF on the trunk (the default on common PBXs). The first-customer test (spec "Order of work" step 7) covers it.

- [ ] **Step 1: Write the failing tests.** Core, in `test_sip_connections.py`:

```python
def test_only_first_six_digits_count():
    stored = hash_check_code("482913")
    assert check_code_matches("482913", stored) is True
    assert check_code_matches("1482913", stored) is False   # 7 digits: no window search
    assert check_code_matches("48291", stored) is False
    assert check_code_matches("", stored) is False
```

  Core, in `test_inbound_calls.py` (`unverified_number` is `+14155550100` on a connection with inbound trunk `ST_in_pbx`; `seed_check_call(number, status, requested_at, code="482913")` stores `hash_check_code(code)`):

```python
async def test_check_arrival_on_connection_trunk_does_not_activate_yet(async_session, unverified_number):
    check = await seed_check_call(unverified_number, status="ringing", requested_at=utcnow())
    out = await open_inbound_call(async_session, attrs(dialed="+14155550100", trunk_id="ST_in_pbx"))
    assert out == CheckArrived(unverified_number.id, check.id)
    assert unverified_number.provisioning_state == "unverified"
    assert (await last_check_event(check)).payload == {"outcome": "arrived"}

async def test_right_code_activates(async_session, unverified_number):
    check = await seed_check_call(unverified_number, status="in_progress", requested_at=utcnow())
    assert await finish_check(async_session, CheckArrived(unverified_number.id, check.id), "482913", now=NOW) is True
    assert unverified_number.provisioning_state == "active"
    assert unverified_number.acquired_at == NOW
    assert (await last_check_event(check)).payload == {"outcome": "passed"}

@pytest.mark.parametrize("digits", ["000000", "", "48291"])
async def test_wrong_or_missing_code_does_not_activate(async_session, unverified_number, digits):
    check = await seed_check_call(unverified_number, status="in_progress", requested_at=utcnow())
    assert await finish_check(async_session, CheckArrived(unverified_number.id, check.id), digits, now=NOW) is False
    assert unverified_number.provisioning_state == "unverified"
    assert (await last_check_event(check)).payload == {"outcome": "code_not_received"}

async def test_check_arrival_on_other_trunk_does_not_activate(async_session, unverified_number):
    check = await seed_check_call(unverified_number, status="ringing", requested_at=utcnow())
    out = await open_inbound_call(async_session, attrs(dialed="+14155550100", trunk_id="ST_in_shared"))
    assert out == Rejected("pbx_check_wrong_trunk")
    assert unverified_number.provisioning_state == "unverified"
    assert (await last_check_event(check)).payload == {"outcome": "not_authenticated"}

async def test_arrival_without_check_in_flight_is_dropped(async_session, unverified_number):
    out = await open_inbound_call(async_session, attrs(dialed="+14155550100", trunk_id="ST_in_pbx"))
    assert out == Rejected("unknown_number")
    assert unverified_number.provisioning_state == "unverified"
    assert await count_rows(async_session, Call) == 0

async def test_arrival_after_window_is_dropped(async_session, unverified_number):
    await seed_check_call(unverified_number, status="ringing", requested_at=utcnow() - timedelta(seconds=91))
    out = await open_inbound_call(async_session, attrs(dialed="+14155550100", trunk_id="ST_in_pbx"))
    assert out == Rejected("unknown_number")

async def test_arrival_for_finished_check_is_dropped(async_session, unverified_number):
    await seed_check_call(unverified_number, status="no_answer", requested_at=utcnow())
    out = await open_inbound_call(async_session, attrs(dialed="+14155550100", trunk_id="ST_in_pbx"))
    assert out == Rejected("unknown_number")
```

  Voicebot, in `test_agent_inbound.py` (`fake_room` emits `sip_dtmf_received` with a `SipDTMF`-like object; `check_ctx` is a fake `JobContext` whose `open_inbound_call` returns `CheckArrived`):

```python
async def test_collect_check_digits_returns_first_six(fake_room):
    task = asyncio.create_task(collect_check_digits(fake_room, seconds=1))
    for digit in "1482913":
        fake_room.emit("sip_dtmf_received", dtmf(digit))
    assert await task == "148291"

async def test_collect_check_digits_times_out_with_what_it_has(fake_room):
    task = asyncio.create_task(collect_check_digits(fake_room, seconds=0.05))
    fake_room.emit("sip_dtmf_received", dtmf("4"))
    assert await task == "4"

async def test_check_passed_plays_line_then_deletes_room(check_ctx, session, finish_check_mock):
    finish_check_mock.return_value = True
    assert await open_inbound_from_room(check_ctx, ...) is None
    session.say.assert_awaited_once_with(SPOKEN_LINES["en"].pbx_check, allow_interruptions=False)
    check_ctx.delete_room.assert_awaited_once()

async def test_check_failed_skips_line_and_deletes_room(check_ctx, session, finish_check_mock):
    finish_check_mock.return_value = False
    assert await open_inbound_from_room(check_ctx, ...) is None
    session.say.assert_not_awaited()
    check_ctx.delete_room.assert_awaited_once()

async def test_check_line_failure_still_deletes_room(check_ctx, session, finish_check_mock):
    finish_check_mock.return_value = True
    session.say.side_effect = RuntimeError("tts down")
    assert await open_inbound_from_room(check_ctx, ...) is None
    check_ctx.delete_room.assert_awaited_once()
```

- [ ] **Step 2: Run.** `cd core && uv run pytest tests/test_sip_connections.py tests/test_inbound_calls.py -v`; `cd voicebot && uv run pytest tests/test_agent_inbound.py -v`. Expected: FAIL on the new tests.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Same commands. Expected: PASS.
- [ ] **Step 5: Commit.**

```bash
git add core/hailhq/core/sip_connections.py core/hailhq/core/inbound_calls.py \
  voicebot/hailhq/voicebot/agent.py core/tests/test_sip_connections.py \
  core/tests/test_inbound_calls.py voicebot/tests/test_agent_inbound.py
git commit -m "feat(inbound): mark a connected number checked when Hail's test call and code arrive on its trunk"
```

## Task A13: Ownership check, outbound side

**Files:**
- Modify: `api/hailhq/api/connected_numbers.py`
- Modify: `voicebot/hailhq/voicebot/agent.py` (`entrypoint` 2205+: a dispatch with `pbx_check_send` runs only the code sender)
- Test: `api/tests/test_connected_numbers.py`, `voicebot/tests/test_pbx_check_sender.py` (new)

**Interfaces:**
- Consumes: `claim_pool_number`, `release_pool_reservation` (`core/hailhq/core/pool.py`), `has_funds` (`hailhq.core.billing`), `sells_in` (`hailhq.core.telephony_catalog`), `check_handover_allowed` (`hailhq.core.compliance_gate`), `voice_route(db, number)`, `LiveKitClient.create_room`, `dispatch_agent`, `create_sip_participant(..., wait_until_answered=True, ringing_timeout_seconds=CHECK_RING_SECONDS)`, `delete_room`, `notify_usage_event_recorded` (`hailhq.core.internal_webhook`); `CHECK_CODE_SECONDS`, `CHECKS_PER_DAY`, `CHECK_WINDOW` (Task A3); `check_in_flight`, `hash_check_code`, `CHECK_CODE_META` (Task A12).
- Produces in `connected_numbers.py`:

```python
@dataclass(frozen=True)
class CheckResult:
    state: Literal["active", "unverified"]
    reason: Literal["no_call_arrived", "not_authenticated", "code_not_received"] | None
    call_id: UUID | None

CHECK_LINE_SECONDS = 6   # after a pass, how long the outbound leg stays up so the line can play
CHECK_RESULT_WAIT_SECONDS = CHECK_CODE_SECONDS + 3   # 18: after the dial returns, wait for the inbound side's result
CHECK_MAX_DURATION_SECONDS = 90   # the check Call's max_duration_seconds, same as CHECK_WINDOW

async def check_connected_number(
    db: AsyncSession, lk: LiveKitClient, number: PhoneNumber, *, now: datetime | None = None
) -> CheckResult
```

- Produces in the voicebot:

```python
CHECK_SEND_DELAY_SECONDS = 1   # after "arrived", before the send: the inbound job must be listening

async def send_check_code(ctx: JobContext, code: str) -> None
# waits for the SIP participant's sip.callStatus == "active" (at most CHECK_RING_SECONDS + 5 s),
# sleeps CHECK_SEND_DELAY_SECONDS, then sends each digit through make_agent_send_dtmf(ctx)
# with DIGIT_DELAY_SECONDS (core send_dtmf) between digits, once: only the first 6
# key presses count and a check call has one result. Errors are logged; it never raises.
```

- `entrypoint`: when the dispatch metadata has `pbx_check_send`, run `send_check_code` and return. No `AgentSession`, no Call row update, no usage: the API finalizes the check call.
- Steps inside `check_connected_number`:
  1. `org_lock`, refresh. `active` -> `CheckResult("active", None, None)`. Not `pbx` or not `unverified` -> `not_unverified`.
  2. `check_in_flight(db, number.id, now)` -> `check_in_progress`. `CHECKS_PER_DAY` check calls for this number with `requested_at > now - 24h` -> `check_limit`.
  3. `has_funds` false -> `no_funds`. Number's country not `sells_in`, or `check_handover_allowed(db, org, e164)` not allowed -> `not_callable`.
  4. `claim_pool_number(db, capability="voice")`; `None` -> `pool_exhausted`.
  5. `code = f"{secrets.randbelow(10**6):06d}"`. Insert the check `Call`: `organization_id` the number's org, `from_number_id`/`from_e164` the pool number, `to_e164` the claimed number, `provider` the pool number's carrier, `direction="outbound"`, `status="ringing"`, `voice_config={}`, `max_duration_seconds=CHECK_MAX_DURATION_SECONDS`, `metadata_={CALL_META_BILLED: True, CALL_META_FROM_POOL: True, CHECK_CALL_META: str(number.id), CHECK_CODE_META: hash_check_code(code)}`; pool `reserved_call_id = call.id`; commit (drops the org lock before the dial). The code in clear never reaches the database.
  6. `trunk, headers = await voice_route(db, pool_number)`; `create_room(call.id)`; `dispatch_agent(room_name=room, agent_name="hail-voicebot", metadata={"call_id": str(call.id), "pbx_check_send": code})`; then dial with `wait_until_answered=True`. Record `answered_at` when it returns; a `SipCallError` means not answered.
  7. Poll `call_events` for `CHECK_EVENT_KIND` on the call every 0.25 s for up to `CHECK_RESULT_WAIT_SECONDS`. `arrived` does not stop the poll; it stops on `passed`, `code_not_received` or `not_authenticated`. On `passed`, sleep `CHECK_LINE_SECONDS`.
  8. `delete_room`. Finalize in one transaction: answered -> `completed` / `normal_hangup`, else `no_answer` / `user_unavailable`; a setup error -> `failed` / the matching `*_failed` reason; `ended_at`; one `state_change` `CallEvent`; `release_pool_reservation`; when answered, `UsageEvent(channel="voice", units=<answered to hang-up in ms>, ref=str(call.id))`. Commit, then `notify_usage_event_recorded`. No customer webhook is sent for a check call.
  9. Result, read after the finalize commit (the event and the number row once more, so a pass that lands late still counts): row `active` or event `passed` -> `("active", None)`; `code_not_received` (or only `arrived`) -> `("unverified", "code_not_received")`; `not_authenticated` -> `("unverified", "not_authenticated")`; none -> `("unverified", "no_call_arrived")`.
- Timing: ring 30 + result wait 18 + line 6 = 54 s, under `CHECK_WINDOW` and `CHECK_MAX_DURATION_SECONDS` (90). A failed check holds a pool line for up to 90 s.

- [ ] **Step 1: Write the failing tests.** A fixture monkeypatches `connected_numbers.CHECK_LINE_SECONDS` to 0.01 and `connected_numbers.CHECK_RESULT_WAIT_SECONDS` to 0.5 for every test except `test_check_timing_fits_the_window`, which reads the real constants. `lk` is a mock. The inbound side is faked by `inbound_says(*outcomes, activate=False, later=None, delay=0.05)`, a `create_sip_participant` side effect: it writes one `pbx_check` event per outcome at once; with `later`, a background task writes that outcome after `delay` seconds; with `activate=True`, it sets the row `active` with the `passed` event (what `finish_check` does on a pass):

```python
def test_check_timing_fits_the_window():
    assert CHECK_RESULT_WAIT_SECONDS == CHECK_CODE_SECONDS + 3
    assert CHECK_RING_SECONDS + CHECK_RESULT_WAIT_SECONDS + CHECK_LINE_SECONDS < CHECK_WINDOW.total_seconds()
    assert CHECK_MAX_DURATION_SECONDS == CHECK_WINDOW.total_seconds() == 90

async def test_check_passes_and_bills_one_answered_call(async_session, lk, pbx_number, pool_number):
    lk.create_sip_participant.side_effect = inbound_says("arrived", "passed", activate=True)
    result = await check_connected_number(async_session, lk, pbx_number)
    assert (result.state, result.reason) == ("active", None)
    call = await async_session.get(Call, result.call_id)
    assert call.status == "completed"
    assert call.answered_at is not None
    assert call.max_duration_seconds == 90
    assert call.metadata_["pbx_check_number_id"] == str(pbx_number.id)
    assert (await usage_rows(call.id))[0].units > 0
    assert pool_number.reserved_call_id is None
    lk.create_sip_participant.assert_awaited_once()
    assert lk.create_sip_participant.await_args.kwargs["ringing_timeout_seconds"] == 30

async def test_code_is_never_stored_in_clear(async_session, lk, pbx_number, pool_number, monkeypatch):
    monkeypatch.setattr(connected_numbers.secrets, "randbelow", lambda n: 482913)
    result = await check_connected_number(async_session, lk, pbx_number)
    call = await async_session.get(Call, result.call_id)
    assert "482913" not in json.dumps(call.metadata_)
    assert call.metadata_[CHECK_CODE_META] == hash_check_code("482913")

async def test_check_sends_code_before_the_dial(async_session, lk, pbx_number, pool_number, monkeypatch):
    monkeypatch.setattr(connected_numbers.secrets, "randbelow", lambda n: 482913)
    await check_connected_number(async_session, lk, pbx_number)
    assert lk.dispatch_agent.await_args.kwargs["metadata"]["pbx_check_send"] == "482913"
    assert [c[0] for c in lk.mock_calls if c[0] in ("dispatch_agent", "create_sip_participant")] == [
        "dispatch_agent", "create_sip_participant"]

async def test_check_without_arrival_reports_no_call(async_session, lk, pbx_number, pool_number):
    result = await check_connected_number(async_session, lk, pbx_number)   # answered, no event
    assert (result.state, result.reason) == ("unverified", "no_call_arrived")
    assert len(await usage_rows(result.call_id)) == 1

async def test_check_unanswered_is_not_billed(async_session, lk, pbx_number, pool_number):
    lk.create_sip_participant.side_effect = SipCallError(480, "no answer")
    result = await check_connected_number(async_session, lk, pbx_number)
    assert (result.state, result.reason) == ("unverified", "no_call_arrived")
    assert (await async_session.get(Call, result.call_id)).status == "no_answer"
    assert await usage_rows(result.call_id) == []

async def test_check_wrong_trunk_reports_not_authenticated(async_session, lk, pbx_number, pool_number):
    lk.create_sip_participant.side_effect = inbound_says("not_authenticated")
    result = await check_connected_number(async_session, lk, pbx_number)
    assert (result.state, result.reason) == ("unverified", "not_authenticated")

async def test_check_code_not_received_reports_it(async_session, lk, pbx_number, pool_number):
    lk.create_sip_participant.side_effect = inbound_says("arrived", "code_not_received")
    result = await check_connected_number(async_session, lk, pbx_number)
    assert (result.state, result.reason) == ("unverified", "code_not_received")

async def test_poll_does_not_stop_on_arrived(async_session, lk, pbx_number, pool_number, sleeps):
    # "arrived" at the dial, "passed" 0.05 s later from a background task.
    # `sleeps` records each asyncio.sleep argument in connected_numbers and still sleeps.
    lk.create_sip_participant.side_effect = inbound_says("arrived", later="passed", activate=True)
    result = await check_connected_number(async_session, lk, pbx_number)
    assert result.state == "active"
    assert connected_numbers.CHECK_LINE_SECONDS in sleeps   # the line only plays when the poll saw "passed"

async def test_late_pass_still_counts(async_session, lk, pbx_number, pool_number, monkeypatch):
    lk.create_sip_participant.side_effect = inbound_says("arrived")
    monkeypatch.setattr(connected_numbers, "release_pool_reservation", activates_then_releases(pbx_number))
    result = await check_connected_number(async_session, lk, pbx_number)
    assert result.state == "active"

@pytest.mark.parametrize("arrangement,code", [
    ("check_in_flight", "check_in_progress"),
    ("five_checks_today", "check_limit"),
    ("no_funds", "no_funds"),
    ("empty_pool", "pool_exhausted"),
    ("premium_number", "not_callable"),
])
async def test_check_refused(async_session, lk, pbx_number, arrange, arrangement, code):
    await arrange(arrangement)
    with pytest.raises(ConnectedNumberError) as e:
        await check_connected_number(async_session, lk, pbx_number)
    assert e.value.code == code
    lk.create_room.assert_not_awaited()

async def test_check_of_active_number_does_not_dial(async_session, lk, active_pbx_number):
    assert await check_connected_number(async_session, lk, active_pbx_number) == CheckResult("active", None, None)
    lk.create_room.assert_not_awaited()
```

  Voicebot, in `test_pbx_check_sender.py` (`sleeps` records `asyncio.sleep` arguments; `check_ctx` has one SIP participant already `active`):

```python
async def test_check_sender_sends_code_once(check_ctx, sleeps):
    await send_check_code(check_ctx, "482913")
    digits = [c.kwargs["digit"] for c in check_ctx.room.local_participant.publish_dtmf.await_args_list]
    assert digits == list("482913")
    assert sleeps[0] == CHECK_SEND_DELAY_SECONDS

async def test_check_sender_never_raises(check_ctx, sleeps):
    check_ctx.room.local_participant.publish_dtmf.side_effect = RuntimeError("closed")
    assert await send_check_code(check_ctx, "482913") is None

async def test_check_dispatch_runs_only_the_sender(check_ctx, monkeypatch):
    check_ctx.job.metadata = json.dumps({"call_id": CALL_ID, "pbx_check_send": "482913"})
    sender, session_cls = AsyncMock(), Mock()
    monkeypatch.setattr(agent, "send_check_code", sender)
    monkeypatch.setattr(agent, "AgentSession", session_cls)
    await entrypoint(check_ctx)
    sender.assert_awaited_once_with(check_ctx, "482913")
    session_cls.assert_not_called()
```

- [ ] **Step 2: Run.** `cd api && uv run pytest tests/test_connected_numbers.py -v`; `cd voicebot && uv run pytest tests/test_pbx_check_sender.py -v`. Expected: FAIL on the new tests.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Same commands. Expected: PASS.
- [ ] **Step 5: Commit.**

```bash
git add api/hailhq/api/connected_numbers.py api/tests/test_connected_numbers.py \
  voicebot/hailhq/voicebot/agent.py voicebot/tests/test_pbx_check_sender.py
git commit -m "feat(api): ownership check calls the number from a pool line and sends a key-press code"
```

## Task A14: Account close

**Files:**
- Modify: `api/hailhq/api/org_close.py` (`_release_numbers` 182-255, `close_org_carrier_items` 434+, module docstring step list)
- Test: `api/tests/test_org_close.py`

**Interfaces:**
- Consumes: `remove_connected_number`, `delete_connection` (Tasks A6, A7).
- `_release_numbers`: the row query also selects `provisioning_state == "unverified"`. A `pbx` row is handled before the `CARRIERS` check: `lk is None` counts as `failed` (retried next pass); else `await remove_connected_number(db, lk, number)` and `counts.released += 1`. A `pbx` row is never stored as a refusal.
- New step after `_release_numbers` in `close_org_carrier_items`:

```python
async def _delete_connections(
    db: AsyncSession, org_ids, lk: LiveKitClient | None, counts: CloseCounts
) -> None
# each sip_connections row of the closed orgs with deleted_at NULL and no live number:
# delete_connection(db, lk, conn); lk None or an error -> counts.failed, retried next pass
```

- [ ] **Step 1: Write the failing tests.**

```python
async def test_close_removes_connected_numbers_and_connection(async_session, verifier, lk):
    # closed org: one active and one unverified pbx number on one connection
    await _run(async_session, verifier, lk)
    assert {n.provisioning_state for n in numbers} == {"released"}
    assert conn.deleted_at is not None
    assert {c.args[0] for c in lk.delete_sip_trunk.await_args_list} == {"ST_in_pbx", "ST_out_pbx"}
    assert "release_refused" not in pbx_number.provisioning_metadata

async def test_close_without_livekit_retries_pbx_later(async_session, verifier, closed_org_pbx_number):
    counts = await _run(async_session, verifier, None)
    assert counts.failed == 1
    assert await state_of(closed_org_pbx_number) == "active"
    assert "release_refused" not in closed_org_pbx_number.provisioning_metadata
```

  `_run(session, verifier, lk)` is the file's existing helper; `state_of(n)` re-reads `provisioning_state`.

- [ ] **Step 2: Run.** `cd api && uv run pytest tests/test_org_close.py -v`. Expected: FAIL on the new tests.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `cd api && uv run pytest tests/test_org_close.py tests/test_internal_org_closures.py -v`. Expected: PASS.
- [ ] **Step 5: Commit.**

```bash
git add api/hailhq/api/org_close.py api/tests/test_org_close.py
git commit -m "feat(org-close): remove connected numbers and delete their phone system trunks"
```

## Task A15: Operator command and runbook

Needed because ops step 3 (6 calls at once on one connection) and the first customer setup happen before the public routes (hail PR 2).

**Files:**
- Create: `api/hailhq/api/connected_numbers_ops.py`
- Create: `docs/operations/connected-numbers.md`
- Modify: `CLAUDE.md` (runbook list under "Internal runbooks", one line)
- Test: `api/tests/test_connected_numbers_ops.py`

**Interfaces:**
- Consumes: every service function of Tasks A6, A7, A13.
- Produces: `def main(argv: list[str] | None = None) -> int`, run as `python -m hailhq.api.connected_numbers_ops`:

```text
create  --org <uuid> --label <text> --region <CC> [--pbx-host <host>] [--pbx-port <n>] [--pbx-transport auto|udp|tcp|tls]
        prints: connection id, inbound username, inbound password (once)
add     --connection <uuid> --number <+E.164>     prints: number id, state
check   --number-id <uuid>                        prints: state, reason, check call id
remove  --number-id <uuid>
delete  --connection <uuid>
```

- Exit codes: 0 done; 1 a `ConnectedNumberError` (prints `code` and `field`); 2 bad arguments.
- The PBX password is never logged (stdout only).

- [ ] **Step 1: Write the failing tests** (service functions monkeypatched; no DB or LiveKit):

```python
def test_create_prints_password_once(capsys, monkeypatch):
    monkeypatch.setattr(ops, "create_connection", AsyncMock(return_value=(fake_conn(), "P" * 32)))
    assert main(["create", "--org", ORG, "--label", "Main office", "--region", "US",
                 "--pbx-host", "pbx.example.com"]) == 0
    assert capsys.readouterr().out.count("P" * 32) == 1

def test_check_prints_reason(capsys, monkeypatch):
    monkeypatch.setattr(ops, "check_connected_number",
                        AsyncMock(return_value=CheckResult("unverified", "no_call_arrived", CALL)))
    assert main(["check", "--number-id", NUMBER]) == 0
    assert "unverified no_call_arrived" in capsys.readouterr().out

def test_service_error_exits_1(capsys, monkeypatch):
    monkeypatch.setattr(ops, "add_connected_number", AsyncMock(side_effect=ConnectedNumberError("number_taken")))
    assert main(["add", "--connection", CONN, "--number", "+14155550100"]) == 1
    assert "number_taken" in capsys.readouterr().out

def test_bad_region_is_an_argument_error():
    assert main(["create", "--org", ORG, "--label", "Main office", "--region", "usa"]) == 2
```

  `ops` is `hailhq.api.connected_numbers_ops`; the DB session and LiveKit client it opens are monkeypatched to `AsyncMock()` in a fixture.

- [ ] **Step 2: Run.** `cd api && uv run pytest tests/test_connected_numbers_ops.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the command.
- [ ] **Step 4: Write the runbook** `docs/operations/connected-numbers.md` (one screen). Sections, in order:
  1. "Before any connected number exists": hail-website PR 1 (the `pbx` price) is deployed; the dispatch rule `hail-inbound` lists no trunks. Click path: LiveKit Cloud console > Telephony > Dispatch rules > `hail-inbound` > Edit > remove every trunk under Trunks > Save. Mark it "not checked against the live console".
  2. The commands above, run on the API host from the compose project directory: `docker compose exec api python -m hailhq.api.connected_numbers_ops <command> ...`.
  3. What the customer enters in their PBX: SIP server (the LiveKit project's SIP URI from the LiveKit console), username, password, codecs PCMU or PCMA, dialed number as `+E.164`.
  4. Check results and what each means: `no_call_arrived` (PBX rule or trunk wrong), `not_authenticated` (the call came without the connection's credentials), `code_not_received` (the call arrived but the key-press code did not: the PBX drops DTMF, or a digit was pressed before the code).
  5. Limits: 5 checks per number per 24 h; `unverified` rows removed after 7 days; the check call is billed as a normal outbound call.
  6. The manual LiveKit probe result from Task A2 step 4.
  7. The 6-calls-at-once test (spec "Order of work" step 3): voicebot CPU stays below 70%, and the LiveKit adaptive interruption requests per minute stay below the project limit (spec "Risks and checks").
  Add to `CLAUDE.md` after the `voice-llm.md` entry: `[connected-numbers.md](docs/operations/connected-numbers.md) (a customer's phone system on its own LiveKit trunks: ops commands, dispatch rule, ownership check).`
- [ ] **Step 5: Run.** `cd api && uv run pytest tests/test_connected_numbers_ops.py -v`. Expected: PASS.
- [ ] **Step 6: Full suite before the PR.** From the repo root: `uvx ruff==0.16.10 check .`, `uvx black==26.10.0 --check .`; then `cd core && uv run pytest tests -q`, `cd api && uv run pytest tests -q`, `cd voicebot && uv run pytest tests -q`. Expected: all PASS.
- [ ] **Step 7: Commit.**

```bash
git add api/hailhq/api/connected_numbers_ops.py api/tests/test_connected_numbers_ops.py \
  docs/operations/connected-numbers.md CLAUDE.md
git commit -m "docs(ops): connected numbers runbook and operator command"
```

---

# Part B: hail-website PR 1 (merges after Part A is deployed)

Repo: hail-website. Branch: `feat/pbx-connected-number-billing`, from `origin/main`. Test runner: Vitest (`pnpm vitest run <file>`). Before the PR: `pnpm vitest run`, `pnpm tsc --noEmit`, `pnpm lint`.

## Task B1: The `pbx` price entry and `voiceCallCents`

**Files:**
- Modify: `lib/private-rates.ts` (new export after `VOICE_PRICES`, line 175; `voiceCallCents` 189-203)
- Test: `lib/__tests__/private-rates.test.ts`

**Interfaces:**
- Produces:

```ts
/** Connected phone systems (hail `phone_numbers.provider = 'pbx'`). One place to change these prices. */
export const PBX_PRICES = {
  cents_per_minute: 10,          // the call, every direction and country
  handover_cents_per_minute: 10, // each answered handover leg, on top of the call
  monthly_cents: 0,
  setup_cents: 0,
} as const;
export const PBX_PROVIDER = "pbx";
export const isPbx = (provider: string | null | undefined) => provider === PBX_PROVIDER;

export function voiceCallCents(
  durationMs: number,
  legs: { cents: number; countryCode: string | null; lineType: string | null }[],
  recording: boolean,
  provider?: string,
): number
// isPbx(provider): roundUpTenthCent(PBX_PRICES.cents_per_minute * minutes + Σ legs.cents + recording surcharge)
// The pbx leg cents are already customer prices (Task B2 priceLegs): no markup, no minimum.
```

- `PBX_PRICES` stays outside `RATES_CENTS_PER_UNIT`: `lib/__tests__/product-jsonld.test.ts` fails on any new key in that block until it is mapped to an offer, and the public price line is a later PR. The recording surcharge reuses `recording_v1_cents_per_ms` on the call's length only (never on handover time).

- [ ] **Step 1: Write the failing tests.**

```ts
describe("connected phone system price", () => {
  const handover = (cents: number) => ({ cents, countryCode: null, lineType: null });
  it("charges 10 cents a minute", () => {
    expect(voiceCallCents(60_000, [], false, "pbx")).toBe(10);
    expect(voiceCallCents(90_000, [], false, "pbx")).toBe(15);
  });
  it("adds 1 cent a minute of call length for recording", () => {
    expect(voiceCallCents(60_000, [], true, "pbx")).toBe(11);
    expect(voiceCallCents(60_000, [handover(10)], true, "pbx")).toBe(21);
  });
  it("adds the handover legs' cents with no markup or minimum", () => {
    expect(voiceCallCents(60_000, [handover(10)], false, "pbx")).toBe(20);
    expect(voiceCallCents(120_000, [handover(5)], false, "pbx")).toBe(25);
  });
  it("charges the handover 10 cents a minute", () => {
    expect(PBX_PRICES.handover_cents_per_minute).toBe(10);
  });
  it("has no monthly or setup fee", () => {
    expect(PBX_PRICES.monthly_cents).toBe(0);
    expect(PBX_PRICES.setup_cents).toBe(0);
  });
  it("leaves carrier calls unchanged", () => {
    expect(voiceCallCents(60_000, [], false, "twilio")).toBe(voiceCallCents(60_000, [], false));
  });
});
```

- [ ] **Step 2: Run.** `pnpm vitest run lib/__tests__/private-rates.test.ts`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same file and `pnpm vitest run lib/__tests__/product-jsonld.test.ts`. Expected: PASS.
- [ ] **Step 5: Commit.**

```bash
git add lib/private-rates.ts lib/__tests__/private-rates.test.ts
git commit -m "feat(billing): connected phone system price, 10 cents a minute plus 10 per handover minute"
```

## Task B2: Rater, estimate and settle for `pbx` calls

**Files:**
- Modify: `lib/voice-cost-rater.ts` (`priceLegs` 147-182, `estimateCallCents` 247-255, `settleOne` call to `voiceCallCents`)
- Modify: `lib/usage-rater.ts` (voice pricing, lines 117-125)
- Test: `lib/__tests__/voice-cost-rater.test.ts`, `lib/__tests__/usage-rater-voice.test.ts`

**Interfaces:**
- Consumes: `isPbx`, `PBX_PRICES`, `voiceCallCents(..., provider)` (Task B1).
- `priceLegs`: when `isPbx(call.provider)`, runs first (before the `table === null` branch) and never calls `tableRate`. Each leg gives `{ cents, countryCode: null, lineType: null, source: "real" }`:
  - `role: "call"`: `cents: 0` (the call minutes are in `voiceCallCents`).
  - `role: "handover"` with `answered_at`: `cents = PBX_PRICES.handover_cents_per_minute * (end - answered_at) / 60_000`, where `end` is `left_at`, or the last leg's `left_at` when it is null; never below 0. Pro rata by the millisecond, like the call.
  - `role: "handover"` with no `answered_at` (nobody picked up): `cents: 0`.
  - No legs: `[]`.
- `estimateCallCents` passes `call.provider` to `voiceCallCents`.
- `settleOne` computes `final` with `estimateCallCents(table, found.call, found.legs, row.units, isRecordingRef(row.ref))`, so one function picks the `pbx` or carrier price.
- `usage-rater.ts`: a found call with `isPbx(call.call.provider)` is always priced with `estimateCallCents` (it needs no table), never with `heldPriceCents` or `rateUsageCents`.

- [ ] **Step 1: Write the failing tests.** `T0` and `at(seconds)` are the file's existing time helpers.

```ts
const PBX_CALL: CallRow = { ...CALL, provider: "pbx", direction: "inbound",
  from_e164: "101", to_e164: "+14155550100", number_country: "US", number_type: "local" };
const pbxLeg = (over: Partial<LegRow> = {}) =>
  leg({ provider: "pbx", direction: "inbound", to_e164: "+14155550100", cost_status: "found", cost_usd: "0", ...over });
// answered 60 s: at(65) to at(125)
const pbxHandover = (over: Partial<LegRow> = {}) =>
  pbxLeg({ role: "handover", direction: "outbound", to_e164: "800", joined_at: at(60),
           answered_at: at(65), left_at: at(125), ...over });

it("never prices a pbx leg from the rate table", () => {
  const expected = [{ cents: 0, countryCode: null, lineType: null, source: "real" },
                    { cents: 10, countryCode: null, lineType: null, source: "real" }];
  expect(priceLegs(TABLE, PBX_CALL, [pbxLeg(), pbxHandover()], 300_000)).toEqual(expected);
  expect(priceLegs(null, PBX_CALL, [pbxLeg(), pbxHandover()], 300_000)).toEqual(expected);
});
it("prices an unanswered pbx handover dial at 0", () => {
  expect(priceLegs(TABLE, PBX_CALL, [pbxHandover({ answered_at: null })], 300_000)[0].cents).toBe(0);
});
it("prices a pbx call with no legs without the table", () => {
  expect(priceLegs(TABLE, PBX_CALL, [], 60_000)).toEqual([]);
  expect(estimateCallCents(null, PBX_CALL, [], 60_000, false)).toBe(10);
});
it("charges an answered pbx handover 10 cents a minute on top", () => {
  // 2-minute call (20) + 1 answered handover minute (10)
  expect(estimateCallCents(TABLE, PBX_CALL, [pbxLeg({ left_at: at(125) }), pbxHandover()], 120_000, false)).toBe(30);
  // recording: + 2 (call length only)
  expect(estimateCallCents(TABLE, PBX_CALL, [pbxLeg({ left_at: at(125) }), pbxHandover()], 120_000, true)).toBe(32);
});
it("runs an open pbx handover leg to the last leg's end", () => {
  // left_at null; the call leg left at(185): 120 s answered -> 20
  expect(priceLegs(TABLE, PBX_CALL, [pbxLeg({ left_at: at(185) }), pbxHandover({ left_at: null })], 180_000)[1].cents).toBe(20);
});
it("settles a pbx call with no adjustment", async () => {
  // fakeClient gains an optional third argument: the call row its "FROM calls c" query returns.
  // 5-minute call (50) + 1 handover minute (10) = 60
  const { ledger, settlements } = fakeClient([candidate("60")], [pbxLeg({ left_at: at(305) }), pbxHandover()], PBX_CALL);
  expect(await settleVoiceCosts()).toEqual({ settled: 1, review: 0 });
  expect(ledger).toEqual([]);
  expect(settlements[0]).toEqual(["ue-1", "org-1", 0, false]);
});
```

  In `lib/__tests__/usage-rater-voice.test.ts`. `fakeClient` gains a third argument, the rows its `FROM call_legs` query returns. A `pbx` call always has legs (the voicebot writes them before the usage event), so its usage row is picked even when the table is down (`legs: true`):

```ts
const PBX_CALL = { ...CALL, provider: "pbx", direction: "inbound", from_e164: "101", to_e164: "+14155550100" };
const T0 = new Date("2026-11-18T10:00:00Z");
const at = (s: number) => new Date(T0.getTime() + s * 1000);
const pbxLegRow = (over = {}) => ({ call_id: CALL_ID, role: "call", provider: "pbx", direction: "inbound",
  to_e164: "+14155550100", joined_at: T0, answered_at: T0, left_at: at(60), cost_status: "found", cost_usd: "0", ...over });

it("charges a pbx call the pbx price when the rate table is down", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response("", { status: 503 })));
  const { client, debits } = fakeClient(
    [{ ...voice(`${CALL_ID}:recording-v1`), units: 60_000, legs: true }], [PBX_CALL], [pbxLegRow()]);
  vi.mocked(pool.connect).mockResolvedValue(client as never);
  await rateUnpricedUsage();
  expect(debits[0][2]).toBe(-11);   // 10 + 1 recording
});
it("charges a pbx handover on top when the rate table is down", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response("", { status: 503 })));
  const handover = pbxLegRow({ role: "handover", direction: "outbound", to_e164: "800",
                               joined_at: at(0), answered_at: at(0), left_at: at(60) });
  const { client, debits } = fakeClient(
    [{ ...voice(CALL_ID), units: 60_000, legs: true }], [PBX_CALL], [pbxLegRow(), handover]);
  vi.mocked(pool.connect).mockResolvedValue(client as never);
  await rateUnpricedUsage();
  expect(debits[0][2]).toBe(-20);   // 10 call + 10 handover
});
```

- [ ] **Step 2: Run.** `pnpm vitest run lib/__tests__/voice-cost-rater.test.ts lib/__tests__/usage-rater-voice.test.ts`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same files and `pnpm vitest run lib/__tests__/voice-cost.test.ts`. Expected: PASS.
- [ ] **Step 5: Commit.**

```bash
git add lib/voice-cost-rater.ts lib/usage-rater.ts lib/__tests__/voice-cost-rater.test.ts lib/__tests__/usage-rater-voice.test.ts
git commit -m "feat(billing): rate connected calls and their handovers at the pbx price, never the carrier table"
```

## Task B3: Call price quote for `pbx`

**Files:**
- Modify: `lib/voice-cost-rater.ts` (`quoteCallCents` 290-318)
- Modify: `app/api/internal/voice-rates/quote/route.ts` (`bodySchema` 13-29, the `table === null` 503)
- Test: `app/api/internal/voice-rates/quote/__tests__/route.test.ts`

**Interfaces:**
- `quoteCallCents(table: VoiceRateRow[] | null, legs, numbers, recording)`: when every leg has `isPbx(leg.carrier)`, return without reading the table:

```ts
// legs[0] is the call; each further leg is a handover dial (hail _handover_cap sends [main] and [main, second])
centsPerMinute: voiceCallCents(60_000, legs.slice(1).map(() => (
  { cents: PBX_PRICES.handover_cents_per_minute, countryCode: null, lineType: null })), recording, "pbx"),
centsPerCall: 0,
```

  So hail holds 10 cents a minute for the call, and 20 cents a minute while a handover person is on the line (11 and 21 with recording). A non-`pbx` quote with `table === null` still throws; the route keeps its 503 for it.
- Route `bodySchema`: `number_e164` accepts E.164, or, only when `carrier === "pbx"`, an extension matching `/^[0-9*#]{1,15}$/` (a zod `superRefine` on the leg). A `pbx`-only quote answers 200 even when `costs/voice.json` cannot be read.

- [ ] **Step 1: Write the failing tests.**

```ts
const PBX_MAIN = { direction: "inbound", carrier: "pbx", number_e164: "+14155550100", caller_e164: "101" };
const PBX_HANDOVER = { direction: "outbound", carrier: "pbx", number_e164: "800", caller_e164: "+14155550199" };

it("quotes a pbx call at the pbx price", async () => {
  const res = await post({ organization_id: ORG_ID, recording: false, legs: [PBX_MAIN] });
  expect(res.status).toBe(200);
  expect(await res.json()).toEqual({ cents_per_minute: 10, cents_per_call: 0 });
});
it("quotes a pbx handover at 20 cents a minute", async () => {
  const res = await post({ organization_id: ORG_ID, recording: false, legs: [PBX_MAIN, PBX_HANDOVER] });
  expect(res.status).toBe(200);
  expect(await res.json()).toEqual({ cents_per_minute: 20, cents_per_call: 0 });
});
it("adds recording once per call minute", async () => {
  expect(await (await post({ organization_id: ORG_ID, recording: true, legs: [PBX_MAIN] })).json())
    .toEqual({ cents_per_minute: 11, cents_per_call: 0 });
  expect(await (await post({ organization_id: ORG_ID, recording: true, legs: [PBX_MAIN, PBX_HANDOVER] })).json())
    .toEqual({ cents_per_minute: 21, cents_per_call: 0 });
});
it("still refuses an extension on a carrier leg", async () => {
  const res = await post({ organization_id: ORG_ID, recording: false,
    legs: [{ direction: "outbound", carrier: "twilio", number_e164: "800", caller_e164: "+14155550100" }] });
  expect(res.status).toBe(400);
});
it("quotes pbx without the rate table", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("down"); }));
  const res = await post({ organization_id: ORG_ID, recording: false, legs: [PBX_MAIN, PBX_HANDOVER] });
  expect(res.status).toBe(200);
  expect(await res.json()).toEqual({ cents_per_minute: 20, cents_per_call: 0 });
});
```

  Make the rate table fail the way the file's other "table down" test does (it may mock `fetchVoiceRates` instead of `fetch`).

- [ ] **Step 2: Run.** `pnpm vitest run app/api/internal/voice-rates/quote/__tests__/route.test.ts`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Same command. Expected: PASS.
- [ ] **Step 5: Commit.**

```bash
git add lib/voice-cost-rater.ts app/api/internal/voice-rates/quote/route.ts \
  app/api/internal/voice-rates/quote/__tests__/route.test.ts
git commit -m "feat(billing): quote connected calls and handovers at the pbx price"
```

## Task B4: Monthly fee rater skips `pbx`

**Files:**
- Modify: `lib/monthly-fee-rater.ts` (number loop after the query, lines 252-272)
- Test: `lib/__tests__/monthly-fee-rater.test.ts`

**Interfaces:**
- Consumes: `isPbx` (Task B1).
- The loop skips `isPbx(num.provider)` rows before `numberRenewalCents`: no charge, not counted in `unpriced`. `carriersToFetch` gets the rows without `pbx` rows.

- [ ] **Step 1: Write the failing test.**

```ts
it("skips pbx numbers, active and removed", async () => {
  // rows: one active pbx number, one pbx number with provisioning_state "released" and released_at set
  const result = await runMonthlyFees(...);
  expect(result.unpriced).toBe(0);
  expect(monthlyFeeRunFailed(result)).toBe(false);
  expect(ledgerInserts()).toHaveLength(0);
});
```

- [ ] **Step 2: Run.** `pnpm vitest run lib/__tests__/monthly-fee-rater.test.ts`. Expected: FAIL (`unpriced` 2).
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same file and `pnpm vitest run app/api/internal/monthly-fees/rate/__tests__/route.test.ts`. Expected: PASS.
- [ ] **Step 5: Commit.**

```bash
git add lib/monthly-fee-rater.ts lib/__tests__/monthly-fee-rater.test.ts
git commit -m "fix(billing): no monthly fee run failure on connected numbers"
```

## Task B5: Idle release and dunning skip `pbx`

**Files:**
- Modify: `lib/idle-number-release.ts` (idle query 214-228: add `p.provider` to the select; filter rows in code)
- Modify: `lib/number-dunning.ts` (number rows 174-180: filter after the query, before the catalog fetch)
- Test: `lib/__tests__/idle-number-release.test.ts`, `lib/__tests__/number-dunning.test.ts`

**Interfaces:**
- Consumes: `isPbx` (Task B1).
- Idle release: a `pbx` row is never warned, stored in `number_dunning` or released.
- Dunning: `pbx` rows are left out before the price lookup, so a workspace with a `pbx` number is not skipped for a missing price, and a `pbx` number is never released for low balance.

- [ ] **Step 1: Write the failing tests.**

```ts
// lib/__tests__/idle-number-release.test.ts (window day: 2026-10-24, as the existing warn test)
it("never warns about or releases a pbx number", async () => {
  const { queries } = fakeClient({ idle: [{ organization_id: ORG, id: "n1", e164: "+14155550100", provider: "pbx" }] });
  const send = vi.fn();
  const release = vi.fn();
  const result = await runIdleNumberRelease({ now: new Date("2026-10-24T08:30:00Z"), idleDays: 30, send, release });
  expect(result).toMatchObject({ warned: 0, errors: 0 });
  expect(send).not.toHaveBeenCalled();
  expect(release).not.toHaveBeenCalled();
  expect(queries.some((q) => q.sql.includes("INSERT INTO number_dunning"))).toBe(false);
});

// lib/__tests__/number-dunning.test.ts
it("ignores pbx numbers in low-balance dunning", async () => {
  fakeClient({
    numbers: [
      { organization_id: ORG, id: "n1", e164: "+14155550100", provider: "twilio", country_code: "US", number_type: "local" },
      { organization_id: ORG, id: "n2", e164: "+14155550101", provider: "pbx", country_code: "US", number_type: "local" },
    ],
    balances: [{ organization_id: ORG, balance_cents: "0" }],
    dunning: [],
    owners: [{ email: "owner@example.com", name: "Sam Lee" }],
  });
  const send = vi.fn();
  const release = vi.fn();
  const result = await runNumberDunning({ now: NOW, send, release });
  expect(result).toMatchObject({ warned: 1, errors: 0 });
  expect(send.mock.calls[0][0].vars.numbersList).toBe("+14155550100");
  expect(release).not.toHaveBeenCalled();
});
it("does not skip an org whose only extra number is pbx", async () => {
  // the pbx row has number_type "national": no catalog price; it must be dropped before the price lookup
  fakeClient({
    numbers: [
      { organization_id: ORG, id: "n1", e164: "+14155550100", provider: "twilio", country_code: "US", number_type: "local" },
      { organization_id: ORG, id: "n2", e164: "+14155550101", provider: "pbx", country_code: "US", number_type: "national" },
    ],
    balances: [{ organization_id: ORG, balance_cents: "0" }],
    dunning: [],
    owners: [{ email: "owner@example.com", name: "Sam Lee" }],
  });
  const result = await runNumberDunning({ now: NOW, send: vi.fn(), release: vi.fn() });
  expect(result).toMatchObject({ warned: 1, errors: 0 });
});
```

  The `numbers` and `idle` fake rows gain a `provider` field (the queries now select `p.provider`); existing rows default to `"twilio"` in `fakeClient`.

- [ ] **Step 2: Run.** `pnpm vitest run lib/__tests__/idle-number-release.test.ts lib/__tests__/number-dunning.test.ts`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Same command, then `pnpm vitest run`, `pnpm tsc --noEmit` and `pnpm lint`. Expected: PASS.
- [ ] **Step 5: Commit.**

```bash
git add lib/idle-number-release.ts lib/number-dunning.ts \
  lib/__tests__/idle-number-release.test.ts lib/__tests__/number-dunning.test.ts
git commit -m "fix(numbers): idle release and dunning leave connected numbers alone"
```

---

## Interfaces for plan 2

hail PR 2 (public API, OpenAPI, CLI, MCP, docs) calls only these. All live in `api/hailhq/api/connected_numbers.py` unless named.

| Route in spec section 8 | Calls |
|---|---|
| `POST /v1/sip-connections` | `create_connection(db, lk, organization_id=..., label=..., default_region=..., pbx_host=..., pbx_port=..., pbx_transport=..., pbx_username=..., pbx_password=...) -> (SipConnection, password)` |
| `PATCH /v1/sip-connections/{id}` | `update_connection(db, lk, connection, label=..., pbx_host=..., ...)` (omitted fields stay `UNSET`) |
| `POST /v1/sip-connections/{id}/rotate-password` | `rotate_inbound_password(db, lk, connection) -> str` |
| `DELETE /v1/sip-connections/{id}` | `delete_connection(db, lk, connection)` |
| `POST /v1/sip-connections/{id}/numbers` | `add_connected_number(db, lk, connection, e164) -> PhoneNumber` |
| `POST /v1/sip-connections/{id}/numbers/{number_id}/verify` | `check_connected_number(db, lk, number) -> CheckResult(state, reason, call_id)` |
| `DELETE /v1/sip-connections/{id}/numbers/{number_id}` and `DELETE /v1/numbers/{id}` | `remove_connected_number(db, lk, number)` (already reached through `release_org_number`) |
| Error mapping | `ConnectedNumberError.code` and `.field` (Task A6 list) |
| Secrets in responses | `decrypt_secret` / last 4 characters (Task A3); `SipConnection.inbound_username` |
| Contacts with extensions | `Contact.extension`, `HandoverPerson.extension`; the CHECKs from Task A1 (`ContactCreate`/`ContactPatch` change in plan 2) |

Plan 2 also needs the SIP server host shown to the customer (`<project>.sip.livekit.cloud`). No setting holds it in this plan.
