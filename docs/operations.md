# Operations runbook

Single source of truth for how to develop, deploy, migrate, and release Hail. If you're an AI agent picking up the codebase, **read this first** alongside `CLAUDE.md`.

## Quick reference

| task                           | command                                                                                                                         |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------- |
| Bring up stack (bundled DB)    | `docker compose -f docker-compose.yml -f docker-compose.local.yml up -d`                                                        |
| Bring up stack (managed DB)    | `docker compose up -d`                                                                                                          |
| Bring up stack (prod VM)       | `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d` — pulls images from GHCR; see `docs/setup/vm-deploy.md` |
| Tail one service               | `docker compose logs -f <api\|voicebot\|mcp\|postgres>`                                                                         |
| Run all tests                  | `cd <core\|api\|voicebot\|mcp\|sdk> && uv run pytest` (per suite, **from each dir**)                                            |
| Lint                           | `uvx ruff check .` then `uvx black --check .` (repo root)                                                                       |
| Apply DB migrations            | `docker compose run --rm api alembic upgrade head`                                                                              |
| Regenerate OpenAPI + Go client | see _Development → Regenerating OpenAPI_ below                                                                                  |
| Publish SDK                    | tag `sdk-v<X.Y.Z>` and push (fires `release-sdk.yml`)                                                                           |
| Publish CLI                    | tag `cli-v<X.Y.Z>` and push (fires `release-cli.yml`)                                                                           |
| Cut umbrella release           | tag `v<X.Y.Z>` and push (no workflow; just a marker)                                                                            |

## Local development

### Bringing up the stack

```bash
cp .env.example .env                                       # then fill in keys
pnpm install                                               # husky pre-commit hooks
docker compose \
  -f docker-compose.yml -f docker-compose.local.yml \
  up -d                                                    # postgres + minio + api + voicebot + mcp
docker compose run --rm api alembic upgrade head           # apply schema
# seed an org + api_key + phone_number (see Deployment → DB seed below)
```

### Per-service dev loops (host-side, no Docker)

```bash
cd api      && uv run uvicorn hailhq.api.main:app --reload --port 8080
cd voicebot && uv run python -m hailhq.voicebot.main start
cd mcp      && uv run uvicorn hailhq.mcp.server:app --reload --port 8081
cd cli      && go run . <args>
```

Host-side dev needs `.env` exported into the shell first: `set -a; source .env; set +a` (Pydantic Settings reads `.env` into Settings attrs, but plugin SDKs read `os.environ` directly).

### Tests

CI runs each suite from its own directory — match that locally:

```bash
cd core     && uv run pytest -v
cd api      && uv run pytest -v
cd voicebot && uv run pytest -v
cd mcp      && uv run pytest -v
cd sdk      && uv run pytest -v
cd cli      && go test ./... && go vet ./...
```

Python tests use **testcontainers/postgres** locally (auto-spins a Postgres container) or the `DATABASE_URL` env var when set (CI's path).

### Lint + format

Pre-commit runs `ruff check --fix`, `black`, `gofmt -w`, and `prettier --write` on staged files via husky + lint-staged. Manually:

```bash
uvx ruff check .            # at repo root
uvx black --check .         # at repo root
cd cli && gofmt -l . && go vet ./...
```

### Regenerating OpenAPI + Go CLI client

When API routes change:

```bash
# 1. Boot the API (or just import the app) and dump the spec
cd api && uv run python -c "from hailhq.api.main import app; import sys, yaml; yaml.safe_dump(app.openapi(), sys.stdout, sort_keys=False)" > ../openapi/openapi.yaml

# 2. Regenerate the Go client (consumes openapi.yaml via a build-tagged
#    preprocessor that downgrades to OpenAPI 3.0.3 — oapi-codegen v2
#    doesn't yet parse 3.1's anyOf:[type,null] nullable idiom).
cd cli && make codegen
```

Commit `openapi/openapi.yaml` and `cli/internal/client/client.gen.go` together with the route change.

### Adding a new env var

1. Add the field to `core/hailhq/core/config.py` `Settings` class.
2. Add the env line to `.env.example` under the right provider section (provider-grouped convention).
3. If the value is consumed by code, reference `settings.<field>`. If it's consumed implicitly by a LiveKit plugin reading `os.environ`, the Settings declaration is **documentation only** — the runtime path is `docker compose env_file: .env` exporting it to the container.

### Adding a new provider adapter

New adapters live under `core/hailhq/core/providers/<channel>/<name>.py` and implement that channel's adapter interface (e.g., `VoiceProvider` in `providers/voice/base.py`). `api/` and `voicebot/` must **not** import provider SDKs directly — go through `core`.

## Deployment (self-host)

### Required external accounts

- **Twilio**: account SID + auth token + a phone number with voice capability + an Elastic SIP Trunk (Origination URI → LiveKit's inbound, Termination → Twilio's PSTN).
- **LiveKit Cloud**: project + URL + API key + secret + an outbound SIP trunk (`LIVEKIT_SIP_OUTBOUND_TRUNK_ID`) + an inbound trunk (`LIVEKIT_SIP_INBOUND_TRUNK_ID`, reserved for v1.1).
- **Deepgram** (STT): API key.
- **Cartesia** (primary TTS): API key + a voice ID from the Cartesia voice library.
- **ElevenLabs** (fallback TTS, optional): API key + a voice ID. Used automatically when Cartesia fails, if `ELEVEN_API_KEY` is set.
- **At least one LLM provider**: OpenAI / Gemini / Anthropic API key. The voicebot's mode-A FallbackAdapter chains all three; mode-B uses a caller-provided OpenAI-compatible endpoint per call.

Detailed setup walkthroughs: `docs/setup/twilio.md`, `docs/setup/livekit-cloud.md`, `docs/setup/mcp.md`. For running the whole stack on a single Ubuntu VM with HTTPS + auto-deploy from `main`, see `docs/setup/vm-deploy.md`.

### Authentication

Self-host and managed cloud share the same FastAPI binary, but auth is decided implicitly by what's in the env:

| Mode                    | Trigger                                                                | What hail/api checks                                                                                                                                        |
| ----------------------- | ---------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Self-host** (default) | Operator sets `HAIL_API_KEY` in `.env`                                 | Constant-time compare against the env var. All shared-key requests resolve to the sentinel `organization_id = "self-hosted"` — no DB row, no member lookup. |
| **Managed cloud**       | The auth backend's `apikey` table is migrated into the shared Postgres | Hashes the bearer with `base64url(sha256())` and looks it up; resolves the org via `members.user_id = api_keys.reference_id → members.organization_id`.     |

Both can be active simultaneously — if `HAIL_API_KEY` is set in managed cloud, it acts as a master/admin override that always works.

A managed-cloud user with no `member` row gets a **403 "user not provisioned"** rather than a fabricated org — provisioning is the website's responsibility (via its `user.create.after` hook).

#### Self-host: first-run setup

```bash
# 1) Generate a shared API key — used for BOTH directions:
#    inbound (API checks bearer) + outbound (CLI/MCP/voicebot send it).
HAIL_API_KEY="hk_$(openssl rand -base64 32 | tr -d '/+=' | head -c 40)"
echo "HAIL_API_KEY=$HAIL_API_KEY" >> hail/.env

# 2) Add a phone number bound to the self-host sentinel org id.
export TWILIO_E164='+1XXXXXXXXXX' TWILIO_PN_SID='PNxxxxxxxxxxxxxxxx'
docker compose exec postgres psql -U hail -d hail -c "INSERT INTO phone_numbers (organization_id, e164, country_code, number_type, capabilities, provider, provider_resource_id, provisioning_state, acquired_at) VALUES ('self-hosted', '${TWILIO_E164}', 'US', 'local', ARRAY['voice','sms'], 'twilio', '${TWILIO_PN_SID}', 'active', now());"
```

Save the value of `HAIL_API_KEY` — there's no recovery path. Then `export HAIL_API_KEY=…` in the shell that runs `hail`, or pass it via `--api-key`.

> **`hail login` is managed-cloud only.** It runs the auth backend's device flow against `hail-website` and writes the resulting `hl_live_*` key to `~/.hail/credentials.json`. In self-host there's no website to authorize against — set `HAIL_API_KEY` directly and you're done.

#### `hail auth` subcommands

For interactive sessions on a managed Hail deployment:

- `hail login` — device-authorization flow, persists `~/.hail/credentials.json`.
- `hail auth logout` — delete the local credentials file (idempotent).
- `hail auth token` — print the bare API key; use in scripts as
  `export HAIL_API_KEY=$(hail auth token)`.

Self-hosters typically skip the device flow and set `HAIL_API_KEY`
directly per the bootstrap section above.

#### Phone number pool

Pool numbers are unowned `phone_numbers` rows (`is_pool=TRUE`, `organization_id IS NULL`) that any org without its own active number falls back to on outbound calls. They're claimed atomically (`SELECT … FOR UPDATE SKIP LOCKED`, randomized order to spread carrier wear), bound to one call at a time via `reserved_call_id`, and released on call end. Implementation: `core/hailhq/core/pool.py`; sweeper backstop window is `HAIL_POOL_RELEASE_GRACE_SECONDS`.

Add a Twilio number to the pool with `organization_id` NULL and `is_pool=TRUE` (the CHECK constraint enforces the pairing):

```bash
psql "$DATABASE_URL" -c "INSERT INTO phone_numbers (organization_id, e164, country_code, number_type, capabilities, provider, provider_resource_id, provisioning_state, is_pool, acquired_at) VALUES (NULL, '+1XXXXXXXXXX', 'US', 'local', ARRAY['voice','sms'], 'twilio', 'PNxxxxxxxxxxxxxxxx', 'active', TRUE, now());"
```

The number must be attached to the same Twilio SIP trunk you wired in [Twilio setup](setup/twilio.md) — there's no per-number trunk routing. To grow the pool, repeat the INSERT with a different `e164` / `PN_SID`. To quarantine a misbehaving pool number without deleting it, `UPDATE phone_numbers SET provisioning_state='failed' WHERE e164=...` — the claim query skips non-`active` rows.

Callers can't address a pool number explicitly with `POST /calls`'s `from` field; it's shared, so a caller naming one would cross tenants. The fallback only fires when an org has zero active numbers of its own.

#### Managed cloud

Run `hail login`. The CLI opens `/device` on the website, you approve, and it exchanges the device-flow session for a long-lived `hl_live_*` key minted by the auth backend into the `apikey` table. `hail/api` reads the same table — keys minted in the console work everywhere (CLI, MCP, direct API calls).

### MCP modes

The MCP service (`hail/mcp`) picks one of two modes at boot from env:

| Mode           | Env                                                            | Behaviour                                                                                                                                                                                  |
| -------------- | -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **oauth-rs**   | `HAIL_AUTH_URL` + `MCP_RESOURCE_URL` set, `HAIL_API_KEY` empty | FastMCP rejects unauth requests with `401 WWW-Authenticate: Bearer resource_metadata=…`; tools forward each request's JWT to the API; `.well-known/oauth-protected-resource` is published. |
| **static-key** | `HAIL_API_KEY` set, `HAIL_AUTH_URL` empty                      | No inbound auth; tools use the singleton `HailClient(api_key=HAIL_API_KEY)`; no protected-resource route.                                                                                  |

Both set → boot fails with `ambiguous MCP auth config`. Neither set → boot fails with `MCP auth not configured`. The mode is decided once and cannot change without restart.

The MCP service does not validate JWT signatures — `hail/api` is the single source of JWT-validation truth (`HAIL_AUTH_URL`, `HAIL_AUTH_AUDIENCES`). MCP forwards the bearer onto the outbound call; the API validates and resolves the org.

## Database migrations

Schema lives in `api/migrations/versions/`. Alembic config: `api/alembic.ini`. The `DATABASE_URL` env var overrides `sqlalchemy.url`.

```bash
# Apply all pending
docker compose run --rm api alembic upgrade head
# OR host-side (needs DATABASE_URL exported):
cd api && uv run alembic upgrade head

# Author a new migration (hand-written raw SQL via op.execute)
cd api && uv run alembic revision -m "add foo column"

# Revert one revision
cd api && uv run alembic downgrade -1
```

Migrations are hand-written raw SQL for v1 (no SQLAlchemy `target_metadata` wired into `env.py`). Switch to `--autogenerate` when models become the source of truth.

### Migration discipline

**The deploy workflow runs `alembic upgrade head` against prod on every push to `main`** — there is no human review gate between merging a migration and it executing. Three rules keep this safe; one is enforced by the workflow, two depend on you:

1. **`api/migrations/env.py` caps statement runtime at 120s and lock-acquisition at 5s** via `SET LOCAL statement_timeout` / `SET LOCAL lock_timeout`, issued inside the migration transaction so they apply to every statement the migration runs. `SET LOCAL` (rather than `PGOPTIONS` or session-level `SET`) is deliberate — Neon's `-pooler` endpoint and other PgBouncer transaction-pooled connections reject the libpq startup option and reset session state between transactions; the per-transaction form is the only one that works on all of pooled / unpooled / direct.

   The deploy log also prints `alembic current` and `alembic upgrade head --sql` before applying, so a problem migration is visible in CI output before it touches data.

   If a legitimate migration genuinely needs longer (a backfill, `CREATE INDEX CONCURRENTLY`), bypass the guard by issuing its own `SET LOCAL statement_timeout = '<bigger>'` at the top of the migration body — explicit per-migration, not a global cap raise. Bigger caps on every migration mean a runaway one chews through more wall-clock before aborting.

2. **All destructive shape changes split into expand → backfill → contract across separate releases.** A column drop, a type narrowing, a `NOT NULL` on existing data — none of these can land in the same release as the code that depends on the new shape, because the old containers are still running when the migration starts. Concretely:

   | bad: one release                                | good: three releases                                                                                                            |
   | ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
   | drop `phone_numbers.legacy_field` + remove code | release 1: stop writing `legacy_field` (still readable) → release 2: backfill / verify → release 3: `ALTER TABLE … DROP COLUMN` |
   | rename `calls.requested_at` → `calls.queued_at` | release 1: add new column, dual-write → release 2: backfill, switch reads → release 3: drop old column                          |

3. **Never edit, renumber, or remove a migration that has been applied to prod.** Author a new revision instead — treat any revision at or below the previous deploy's `alembic current` as immutable. Renumbering or history-rewriting an already-deployed migration is the same footgun as editing one: prod's `alembic_version` still holds the old revision id, so the next `alembic upgrade head` either can't locate it (`Can't locate revision '<id>'`) or silently skips the real DDL and collides downstream (`relation "…" already exists`). CI never catches this because test fixtures build the schema via `Base.metadata.create_all`, not alembic. Recovery is manual — apply whatever migrations genuinely never ran on prod, then reconcile the pointer with `alembic stamp <head>` (or `UPDATE alembic_version`) once the schema matches. See the Footguns table.

The expand/contract rule is the one that the workflow cannot enforce. If you break it, auto-deploy ships the foot-gun and the rollback is "revert the code, write a new migration to restore the column, re-deploy" — which is hours of incident, not minutes.

### Cross-migration table ownership

Two services migrate the same Postgres database independently:

| Owner            | Migration tool                                                  | Tables                                                                                                                       |
| ---------------- | --------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| **hail-website** | `pnpm dlx @better-auth/cli migrate` (introspects `lib/auth.ts`) | `users`, `accounts`, `sessions`, `verifications`, `device_codes`, `api_keys`, `organizations`, `members`, `invitations`      |
| **hail/api**     | alembic                                                         | `account_credits`, `usage_events`, `phone_numbers`, `conversations`, `calls`, `call_events`, `idempotency_keys`, `audit_log` |

The website's schema source of truth is `lib/auth.ts`. After any change to that file, run `pnpm dlx @better-auth/cli generate -y` to emit the resulting SQL into `better-auth_migrations/` and commit it (audit trail), then `pnpm dlx @better-auth/cli migrate -y` to apply against the target DB. Both subcommands introspect the auth config; `generate` writes the SQL file, `migrate` runs the same diff against the DB.

Columns in hail/api-owned tables that reference a website-owned table — `organization_id` everywhere, `audit_log.api_key_id` — carry **no foreign-key constraint**. This is deliberate.

**Why no FK:** the CLI regenerates the website schema from a TypeScript config on every relevant version bump. If alembic held a hard FK into `organizations(id)`, any rename or shape change on the website side would either need a coordinated alembic migration in the same release, or it would break the next `alembic upgrade head`. Cross-tool referential integrity is a coordination tax we don't want to pay on every dependency bump.

**What we trade off:**

- No `ON DELETE CASCADE` from the auth side. Deleting an `organization` row in Postgres leaves orphaned rows in `account_credits`, `calls`, etc. v1 doesn't hard-delete orgs; if you start, write a sweep query or soft-delete.
- No DB-level guarantee that `organization_id` points at a real row. The auth flow in `api/hailhq/api/deps.py` validates the org exists on every authenticated request, so application-layer integrity holds for the live path. Bulk inserts from migrations or fixtures need to mind it themselves.

**When to break the rule:** if you add a new hail/api-owned table that joins to another hail/api-owned table (e.g., `call_events.call_id → calls.id`), keep the FK. Both ends are alembic-owned, so the constraint is safe.

### Shared-key sentinel

Shared-key (`HAIL_API_KEY`) requests resolve to `organization_id = 00000000-0000-0000-0000-000000000000` (the nil UUID) — a sentinel, not a real row. Nothing seeds it; nothing reads from `organizations` for that path. Self-host operators can attach `phone_numbers`, `account_credits`, etc. to the sentinel by passing the nil UUID as the org id directly (see _Self-host: first-run setup_ above for the phone-number example).

### Switching the database

Compose ships as two files. `docker-compose.yml` is the deployable base and assumes `DATABASE_URL` reaches a Postgres you bring. `docker-compose.local.yml` is a thin overlay that adds a bundled `postgres` container and merges a `depends_on: postgres` into `api` and `voicebot`. Pick a mode:

**Bundled local Postgres** — layer both files:

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d
```

`.env` should keep the default `DATABASE_URL=postgresql://hail:hail@postgres:5432/hail` (the in-network compose hostname).

**Reset the bundled DB** (lose all data, fresh start):

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml down -v   # -v removes volumes
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d postgres
docker compose -f docker-compose.yml -f docker-compose.local.yml run --rm api alembic upgrade head
# re-seed (Phase above)
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d
```

**Managed Postgres (Neon / Supabase / RDS / …)** — use only the base file:

```bash
# 1. Provision the DB; grab the postgres URL (sslmode=require for most hosted providers)
# 2. Edit .env:
#      DATABASE_URL=postgresql://USER:PASSWORD@HOST/DBNAME?sslmode=require
#    Comment out the bundled-local line.
# 3. Apply migrations
docker compose run --rm api alembic upgrade head
# 4. Re-seed (same SQL as above; replace `docker compose exec -T postgres
#    psql -U hail -d hail -c` with `psql "$DATABASE_URL" -c`)
# 5. Bring up the stack — no `-f docker-compose.local.yml`, so no postgres
#    container is started:
docker compose up -d
```

The migration `0001_initial.py` issues `CREATE EXTENSION IF NOT EXISTS pgcrypto;`. Hosted providers that gate extensions need `pgcrypto` allowed.

## Releases

Tag conventions:

| tag prefix     | what fires                               | what it produces                                                                                      |
| -------------- | ---------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `sdk-v<X.Y.Z>` | `.github/workflows/release-sdk.yml`      | `hail-sdk` on PyPI (trusted publishing — no token)                                                    |
| `cli-v<X.Y.Z>` | `.github/workflows/release-cli.yml`      | GoReleaser → multi-arch binaries on GitHub Releases + Homebrew formula push to `hail-hq/homebrew-tap` |
| `v<X.Y.Z>`     | nothing — no workflow keys off bare `v*` | umbrella marker for "this is the v0.1.0 commit"                                                       |

Service images (`hail-api`, `hail-voicebot`, `hail-mcp`) are **not** published. Self-hosters build from source via `docker compose up`. Publishing them is on the v1.x list.

### Releasing the SDK (Python)

```bash
# 1. Bump version in sdk/pyproject.toml (e.g., 0.1.0 → 0.2.0)
# 2. Commit
# 3. Tag and push
git tag sdk-v0.2.0
git push origin sdk-v0.2.0
# 4. Watch https://github.com/hail-hq/hail/actions
```

Pre-flight (one-time): a PyPI Trusted Publisher must be configured for `hail-sdk` pointing at `hail-hq/hail` repo + `release-sdk.yml` workflow. After the project exists on PyPI, promote the _Pending_ publisher to a normal one (PyPI web UI).

### Releasing the CLI (Go)

```bash
# 1. Commit any CLI / config changes
# 2. Tag and push
git tag cli-v0.2.0
git push origin cli-v0.2.0
# 3. Watch the workflow; on green:
brew update && brew upgrade hail-hq/tap/hail
```

Required secret: `HOMEBREW_TAP_TOKEN` — a fine-grained PAT with **Contents: read+write** on `hail-hq/homebrew-tap`. Set under repo Settings → Secrets → Actions. Without it, the binary build + GitHub Release succeed but the formula push fails.

GoReleaser quirks worth knowing:

- OSS GoReleaser doesn't have the Pro `monorepo:` block. The workflow strips the `cli-` prefix into `GORELEASER_CURRENT_TAG` / `GORELEASER_PREVIOUS_TAG` env vars and passes `--skip=validate` so GoReleaser doesn't reject the env-var-overridden tag (the manual `git rev-parse` and `git diff --exit-code HEAD` steps before GoReleaser preserve the validate checks that _do_ matter).
- Snapshot version template is the literal `0.0.0-snapshot-{{ .ShortCommit }}` (not `incpatch`) because the repo carries non-semver tags like `sdk-v0.0.1` that confuse the parser.

### Cutting an umbrella release

```bash
# 1. Update CHANGELOG.md
# 2. Tick newly-shipped milestones in README.md
# 3. Commit
# 4. Tag (no prefix)
git tag v0.2.0
git push origin v0.2.0
# 5. Optional: GitHub Release page from the tag
gh release create v0.2.0 --repo hail-hq/hail --notes-file CHANGELOG.md
```

## Conventions

- **Commits**: [Conventional Commits](https://www.conventionalcommits.org). `feat(scope): …`, `fix(scope): …`, `chore: …`, `docs(scope): …`, etc.
- **Markdown**: GitHub-flavored only. Binary task-list states `[ ]` / `[x]`; non-GFM `[~]` / `[-]` are not allowed.
- **Python namespaces**:
  - Internal monorepo: `hailhq.*` (PEP 420 implicit namespace; no `hailhq/__init__.py` at the namespace root).
  - External SDK: `hail` — published as `hail-sdk` on PyPI, imports as `import hail`. Standalone — does **not** depend on any `hailhq.*` package.
- **Provider model IDs**: live only in `.env.example`; `Settings` fields default to empty strings. Editing `Settings.<provider>_model = "literal"` is wrong.
- **Tag prefix grammar**: `<package>-v<semver>` for component releases (`sdk-v…`, `cli-v…`); bare `v<semver>` for the umbrella.
- **No Opero references** in any committed file.

## Footguns (every one of these has bitten us)

| symptom                                                                                                     | root cause + fix                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| ----------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `voicebot` container exits showing the typer help menu                                                      | CMD missing the `start` subcommand. Fixed in the Dockerfile; if you fork it, keep `["python", "-m", "hailhq.voicebot.main", "start"]`.                                                                                                                                                                                                                                                                                                             |
| `ModuleNotFoundError: No module named 'hailhq'` after a Docker build                                        | Hatchling wheel config used `packages = ["hailhq/<service>"]` — strips the `hailhq/` prefix. Must be `packages = ["hailhq"]`.                                                                                                                                                                                                                                                                                                                      |
| `exec /opt/venv/bin/uvicorn: no such file or directory`                                                     | Renaming `/app/.venv` → `/opt/venv` between builder and runtime breaks shebangs. Keep the same path in both stages (we use `/app/.venv`).                                                                                                                                                                                                                                                                                                          |
| `RuntimeError: no running event loop` from `aiohttp.ClientSession()`                                        | A FastAPI dep that constructs LiveKit/aiohttp must be `async def`. Sync deps run in a threadpool worker thread with no loop.                                                                                                                                                                                                                                                                                                                       |
| `google.auth.exceptions.DefaultCredentialsError: File  was not found.`                                      | `GOOGLE_GENAI_USE_VERTEXAI=true` with empty `GOOGLE_APPLICATION_CREDENTIALS`. Default is `false`; opt into Vertex by flipping it AND providing creds.                                                                                                                                                                                                                                                                                              |
| `failed to parse tag 'cli-v0.1.0' as semver` from GoReleaser                                                | OSS GoReleaser doesn't handle tag prefixes. The workflow's `Compute GoReleaser current/previous tags` step + `--skip=validate` flag handle it; don't remove either.                                                                                                                                                                                                                                                                                |
| `hailhq-core` references a workspace member, but is not one                                                 | Docker build context lacks the repo-root pyproject. The Dockerfile writes a minimal `/app/pyproject.toml` (workspace stub) inline before `uv sync`.                                                                                                                                                                                                                                                                                                |
| `pytest` from repo root fails with `ImportPathMismatchError`                                                | Run each suite from its own directory. CI does this; replicate locally.                                                                                                                                                                                                                                                                                                                                                                            |
| First `pip install hail-sdk` from a venv with `hailhq.*` already installed shadows imports                  | The SDK is standalone by design. If you mix it with internal packages in the same venv, the `hail` package wins for `import hail` (intended); don't co-install for production.                                                                                                                                                                                                                                                                     |
| Deploy fails at `alembic upgrade head` with `Can't locate revision 'NNNN'` or `relation "…" already exists` | A migration already applied to prod was renumbered or removed by a branch reshuffle / history rewrite, so prod's `alembic_version` diverges from the code (see Migration discipline rule 3). Recover by hand: apply only the migrations that genuinely never ran on prod, then `UPDATE alembic_version SET version_num='<head>'` (or `alembic stamp <head>`) once the schema matches — do **not** re-run `upgrade head`, it re-hits the collision. |

## Inbound email rollout

End-to-end deployment of the inbound-email milestone (umbrella `v0.5.0`,
`sdk-v0.3.0`, `cli-v0.5.0`). Five DB stages + infra + tag-driven
component releases. Stage 1 needs a brief maintenance window (the table
rename is not online-safe); everything else is online.

### Full deployment order (copy-paste cheat-sheet)

```bash
# ── 0. Preflight on main ───────────────────────────────────────────────────
git checkout main && git pull --ff-only origin main
git log -1 --oneline                                  # confirm merge SHA

# ── 1. Stage-1 cutover: stop API, migrate, deploy new image ────────────────
docker compose stop api
docker compose run --rm api alembic upgrade 0006      # rename (DOWNTIME)
docker compose up -d api                              # new image w/ new code
hail email domain list                                # 200 smoke

# ── 2. Stage-2 + Stage-3 migrations (online) ───────────────────────────────
docker compose run --rm api alembic upgrade 0007      # inbound columns + email_attachments
docker compose run --rm api alembic upgrade 0008      # webhook tables
docker compose run --rm api alembic current           # → 0008 (head)

# ── 3. Provision AWS infra (Terragrunt reads .env directly) ───────────────
cd infra
terragrunt init
terragrunt plan
terragrunt apply
# Capture outputs: inbound_mx_record, inbound_bucket, activate_command, lambda_function_arn
#
# First time only: pre-create the state bucket + DynamoDB lock table once
# per AWS account — terragrunt can't bootstrap them. See the comment
# block at the top of infra/terragrunt.hcl for the AWS CLI one-liners.

# ── 4. Activate the SES Receipt Rule Set (manual; one-time per region) ─────
aws sesv2 set-active-receipt-rule-set --rule-set-name hail-inbound-prod-rules
#  ↑ ONLY safe on accounts with no other active rule set; see Stage 4 below.

# ── 5. Publish the MX record (manual at your DNS provider) ─────────────────
#  e.g. mail.hail.so  MX  10  inbound-smtp.us-east-1.amazonaws.com
dig MX mail.hail.so                                   # wait for propagation

# ── 6. Flip the inbound flag in API .env, restart ──────────────────────────
# Add: HAIL_INBOUND_ENABLED=true
#      HAIL_INBOUND_BUCKET=<terraform output inbound_bucket>
#      HAIL_INBOUND_HMAC_SECRET=<same as tfvars>
#      HAIL_WEBHOOK_SECRET_KEY=$(uv run --directory core python -c "from hailhq.core.secret_cipher import generate_key; print(generate_key())")
docker compose up -d api                              # picks up new env

# ── 7. Release SDK 0.3.0 (fires release-sdk.yml → PyPI) ────────────────────
git tag -a sdk-v0.3.0 -m "SDK 0.3.0 — inbound email + webhooks"
git push origin sdk-v0.3.0

# ── 8. Release CLI 0.5.0 (fires release-cli.yml → GitHub Releases + brew) ──
git tag -a cli-v0.5.0 -m "CLI 0.5.0 — hail email list/get; webhooks; domain rename"
git push origin cli-v0.5.0

# ── 9. Umbrella tag marker (no workflow fires) ─────────────────────────────
git tag -a v0.5.0 -m "Hail 0.5.0 — inbound email milestone"
git push origin v0.5.0
gh release create v0.5.0 --notes-file CHANGELOG.md    # optional release page

# ── 10. Smoke test (see "Smoke test sequence" subsection below) ────────────
hail email domain list
dig MX mail.hail.so
# … send a test mail, expect status=received within 30s
hail email list --direction inbound
```

**Decoupling note.** Steps 1–6 can ship days ahead of steps 7–9; until
the flag in step 6 flips, the system is outbound-only and the new CLI
and SDK haven't gone public yet. If anything looks wrong after step 6,
you can roll back by setting `HAIL_INBOUND_ENABLED=false` and
restarting — no migration revert needed, no tag re-spin needed.

Detailed nuance per stage follows.

### Stage 1 — schema rename + new code (coordinated cutover, ~30s downtime)

Migration `0006` renames `sender_domains` → `email_domains`. The new
app code only references the new name, so the rename and the deploy
have to land together. There's no incremental path that doesn't add
view-shim complexity for marginal gain.

```bash
# Take the API offline.
docker compose stop api

# Apply 0006 only.
DATABASE_URL=$DATABASE_URL uv run --directory api alembic upgrade 0006

# Deploy the new image and start.
docker compose up -d api

# Smoke: should 200, list returns same rows under the new name.
hail email domain list
```

Rollback: `alembic downgrade 0005` reverses the rename. The new code
breaks against the old name, so this only works if you also roll back
the image.

### Stage 2 — additive inbound schema (online, anytime after stage 1)

Migration `0007` is pure additive: nullable columns on `emails`, new
`email_attachments` table, action columns on `email_domains`. Old and
new code both run against it cleanly.

```bash
DATABASE_URL=$DATABASE_URL uv run --directory api alembic upgrade 0007
```

**Footgun on large tables.** `emails_inbound_message_id_uq` is a
partial unique index whose `WHERE` is false for every existing row,
but Postgres still scans the whole table to build it. If `emails` is
north of a few million rows, edit the migration to `CREATE INDEX
CONCURRENTLY` via `op.execute` (alembic's `create_index` runs in a
transaction, which blocks the concurrent flag).

### Stage 3 — webhook tables (online, anytime after stage 2)

Migration `0008` adds `webhook_subscriptions` + `webhook_deliveries`.
Pure new tables, no locks on existing tables.

```bash
DATABASE_URL=$DATABASE_URL uv run --directory api alembic upgrade 0008
```

At this point the API is ready to receive inbound mail and fire
webhooks — but nothing's wired up yet on the AWS side, and the inbound
flag is off, so the system is still effectively outbound-only.

### Stage 4 — provision AWS infrastructure

The bare Terraform module under `infra/terraform/` provisions S3, the SES
Receipt Rule + Rule Set, the Lambda, and IAM. A Terragrunt wrapper at
`infra/terragrunt.hcl` adds an S3 remote backend with a DynamoDB lock
table, and pulls every variable from the repo's `.env` so there's no
parallel `tfvars` file to keep in sync.

**One-time bootstrap** per AWS account. Terragrunt's state backend
can't bootstrap itself; create the bucket + lock table once with the
AWS CLI (the AWS CLI doesn't read `.env` like Terragrunt does, so this
one step needs the env exported into your shell). Note the state
backend's region is `HAIL_TERRAFORM_STATE_REGION` — often shared
across deployments and **separate from `AWS_REGION`** (which is where
the SES + Lambda + raw-MIME bucket get provisioned). It falls back to
`AWS_REGION` when unset.

```bash
set -a; source .env; set +a   # exports AWS_PROFILE, AWS_REGION, HAIL_TERRAFORM_*
STATE_REGION=${HAIL_TERRAFORM_STATE_REGION:-$AWS_REGION}

aws --profile $AWS_PROFILE s3api create-bucket \
    --bucket $HAIL_TERRAFORM_STATE_BUCKET \
    --region $STATE_REGION
aws --profile $AWS_PROFILE s3api put-bucket-versioning \
    --bucket $HAIL_TERRAFORM_STATE_BUCKET \
    --versioning-configuration Status=Enabled
aws --profile $AWS_PROFILE dynamodb create-table \
    --table-name $HAIL_TERRAFORM_LOCK_TABLE \
    --attribute-definitions AttributeName=LockID,AttributeType=S \
    --key-schema AttributeName=LockID,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --region $STATE_REGION
```

**Each deploy.** Generate (or rotate) the shared HMAC secret in `.env`
ahead of time — the same value is what the API uses to verify Lambda
POSTs:

```bash
# In .env:
#   HAIL_INBOUND_HMAC_SECRET=<openssl rand -hex 32 output>
#   HAIL_API_URL=https://api.hail.so          # the URL the Lambda calls

cd infra
terragrunt init                # Terragrunt reads .env via run_cmd internally;
terragrunt plan                # no manual `source .env` needed here.
terragrunt apply
```

Outputs to capture:

- `inbound_mx_record` — publish at DNS for `HAIL_MAIL_BASE_DOMAIN`.
- `inbound_bucket` — goes into `HAIL_INBOUND_BUCKET` on the API.
- `activate_command` — the `aws sesv2 set-active-receipt-rule-set ...`
  to run after apply.

**Plain Terraform alternative.** The bare module under `infra/terraform/`
still works with `terraform apply -var ...` if you'd rather skip
Terragrunt. The Terragrunt wrapper is opinionated about remote state +
`.env`-driven inputs; the underlying module is provider-vanilla.

**Manual step: activate the receipt rule set.** Terraform creates the
rule set but does not activate it (SES allows one active rule set per
region per account, and activation is destructive against any
existing one):

```bash
# Greenfield AWS account:
aws sesv2 set-active-receipt-rule-set --rule-set-name hail-inbound-prod-rules

# Account with existing receipt rules: import the existing rule set
# into Terraform state, merge Hail's rule into it via the AWS console,
# or skip the module's aws_ses_receipt_rule resource entirely.
```

**Manual step: publish the MX record.** Whatever `inbound_mx_record`
output prints, e.g.:

```
mail.hail.so  MX  10  inbound-smtp.us-east-1.amazonaws.com
```

DNS propagation is typically minutes; SES won't deliver until the
record is live.

### Stage 5 — flip the inbound flag

Add to the API service `.env`:

```bash
HAIL_INBOUND_ENABLED=true
HAIL_INBOUND_BUCKET=<terraform output>
HAIL_INBOUND_HMAC_SECRET=<same value Terraform got>
HAIL_WEBHOOK_SECRET_KEY=<generate with `uv run --directory core python -c "from hailhq.core.secret_cipher import generate_key; print(generate_key())"`>
```

Restart the API. Until this step, `POST /internal/ses-events` returns
503 even if the Lambda is wired — so steps 1–4 can land days ahead of
step 5 and you keep a free rollback window (no migration revert
required to disable inbound).

### Smoke test sequence

After stage 5, in order:

1. **Routing exists.** Pick an org you control. From the console or
   the API, verify the hail-mail row exists and `inbound_enabled=true`:

   ```bash
   hail email domain list
   ```

2. **MX is live.** From any host with `dig`:

   ```bash
   dig MX mail.hail.so
   # Should answer with inbound-smtp.<region>.amazonaws.com
   ```

3. **Round-trip a test mail.** Send to the org's hail-mail address
   from any external account. Within 30s:

   ```bash
   hail email list --direction inbound
   # Should show the new row with status=received.
   ```

4. **Raw + attachment access.** If the test mail had attachments,
   the listing should include their metadata; fetching:

   ```bash
   hail email get <id> | jq .raw_url
   curl -L -H "Authorization: Bearer $HAIL_API_KEY" "$RAW_URL" > out.eml
   ```

   should download the raw MIME bytes.

5. **Forwarding** (only if `forward_to` is configured on the domain):
   the forward target should receive a copy from
   `forwarder+<org>@mail.hail.so` with the original sender in
   `Reply-To:`. Check `hail email list --direction outbound` — a
   row with `metadata.forwarded_from = <inbound id>` should land
   in `status=sent`.

6. **Webhook delivery** (only if a webhook is configured): the
   target should receive a signed POST. Verify the signature header
   parses, the body shape matches the documented event, and the
   delivery row reaches `status=succeeded`:

   ```bash
   hail webhooks deliveries <subscription_id>
   ```

7. **Bounce/complaint plumbing.** Send a test bounce by addressing a
   mail to `bounce@simulator.amazonses.com` from the verified sender;
   SES generates a bounce notification. The matching outbound row
   should flip to `status=bounced` within a few seconds.

8. **Rate cap.** Set `email_domains.forward_rate_per_hour=1`, then
   send two inbounds with forwarding configured. The second's
   `suppressed_reasons` should include `forward_rate_limit`.

### What can go wrong (failure modes I've seen first-hand or modeled)

- **DNS not propagated.** SES silently drops mail to a domain whose
  MX is wrong. `dig MX` is the first diagnostic.
- **Receipt rule set not activated.** SES accepts mail but doesn't
  route it; no S3 object appears. Re-check `aws sesv2
describe-active-receipt-rule-set`.
- **SES sandbox.** New SES accounts only accept mail from verified
  addresses. The mail's `From:` must be on a verified SES identity
  during sandbox.
- **Private webhook target rejected.** `httpx_post` blocks RFC-1918
  addresses by default. Self-hosters with internal webhook targets
  set `HAIL_WEBHOOK_ALLOW_PRIVATE_NETWORKS=true`.
- **Lambda → API connection refused.** Check the Lambda's
  CloudWatch logs (`/aws/lambda/hail-inbound-prod-ingest`); the
  POST URL must be reachable from AWS over the public internet.
- **Forward loops.** Forwarding to a target on
  `HAIL_MAIL_BASE_DOMAIN` is rejected; forwarding to a target whose
  auto-responder replies to the forwarder will get caught by the
  3-hop counter. If it isn't, raise `HAIL_FORWARD_MAX_HOPS` or
  blacklist the target by clearing `forward_to`.

## SMS abuse monitor & channel suspensions

All orgs share one Hail-owned A2P 10DLC campaign, so one org's high opt-out
rate can get the whole platform throttled by carriers. `AbuseMonitorWorker`
(hourly by default; `HAIL_ABUSE_MONITOR_POLL_SECONDS`) computes each org's
rolling opt-out rate and, past the threshold
(`HAIL_SMS_ABUSE_*` — window, min sends, max rate), inserts a
`channel_suspensions` row. `check_sms_allowed` then blocks that org's
outbound SMS. Thresholds are unvalidated starting guesses — expect to tune
them once real traffic lands.

**Lifting a suspension is manual for now — there is no CLI/route/expiry.**
An auto-suspended org stays blocked until an operator runs raw SQL. Inspect
and lift:

```bash
# See who is suspended and why
psql "$DATABASE_URL" -c \
  "SELECT organization_id, channel, reason, suspended_at FROM channel_suspensions;"

# Lift one org's SMS suspension (re-enables outbound immediately)
psql "$DATABASE_URL" -c \
  "DELETE FROM channel_suspensions WHERE organization_id = '<org-uuid>' AND channel = 'sms';"
```

Before lifting, confirm the opt-out spike was benign (a burst of legitimate
STOPs, not abuse) — otherwise the next hourly tick re-suspends. There is no
cooldown/backoff yet, so a persistently-abusive org will re-trip on the next
run. Tune `HAIL_SMS_ABUSE_MAX_OPT_OUT_RATE` up if you are seeing false
positives.

## Carry-forwards / open work

(See `CHANGELOG.md` "Deferred to v1.x" for the canonical list.)

- LiveKit Egress recording → S3 (currently `recording.py` returns `None`)
- `idempotency_keys` GC sweeper
- Inbound calls (`LIVEKIT_SIP_INBOUND_TRUNK_ID` reserved)
- SMS, Email channels
- `hail bootstrap` admin CLI (closes the manual DB-seed step above)
- CallEvent dedupe across voicebot redispatch
- **Un-suspend tooling for `channel_suspensions`** — auto-suspend has no
  reverse path (see "SMS abuse monitor" above); add a `hail sms suspensions
lift <org>` command / operator route and/or an automatic cooldown column
  so recovery is not raw SQL.
