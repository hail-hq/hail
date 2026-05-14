# Operations runbook

Single source of truth for how to develop, deploy, migrate, and release Hail. If you're an AI agent picking up the codebase, **read this first** alongside `CLAUDE.md`.

## Quick reference

| task                           | command                                                                              |
| ------------------------------ | ------------------------------------------------------------------------------------ |
| Bring up stack (bundled DB)    | `docker compose -f docker-compose.yml -f docker-compose.local.yml up -d`             |
| Bring up stack (managed DB)    | `docker compose up -d`                                                               |
| Tail one service               | `docker compose logs -f <api\|voicebot\|mcp\|postgres>`                              |
| Run all tests                  | `cd <core\|api\|voicebot\|mcp\|sdk> && uv run pytest` (per suite, **from each dir**) |
| Lint                           | `uvx ruff check .` then `uvx black --check .` (repo root)                            |
| Apply DB migrations            | `docker compose run --rm api alembic upgrade head`                                   |
| Regenerate OpenAPI + Go client | see _Development → Regenerating OpenAPI_ below                                       |
| Publish SDK                    | tag `sdk-v<X.Y.Z>` and push (fires `release-sdk.yml`)                                |
| Publish CLI                    | tag `cli-v<X.Y.Z>` and push (fires `release-cli.yml`)                                |
| Cut umbrella release           | tag `v<X.Y.Z>` and push (no workflow; just a marker)                                 |

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
- **ElevenLabs** (TTS): API key + a voice ID from your library.
- **At least one LLM provider**: OpenAI / Gemini / Anthropic API key. The voicebot's mode-A FallbackAdapter chains all three; mode-B uses a caller-provided OpenAI-compatible endpoint per call.

Detailed setup walkthroughs: `docs/setup/twilio.md`, `docs/setup/livekit-cloud.md`, `docs/setup/mcp.md`.

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
docker compose exec -T postgres psql -U hail -d hail -c "INSERT INTO phone_numbers (organization_id, e164, country_code, number_type, capabilities, provider, provider_resource_id, provisioning_state, acquired_at) VALUES ('self-hosted', '${TWILIO_E164}', 'US', 'local', ARRAY['voice','sms'], 'twilio', '${TWILIO_PN_SID}', 'active', now());"
```

Save the value of `HAIL_API_KEY` — there's no recovery path. Then `export HAIL_API_KEY=…` in the shell that runs `hail`, or pass it via `--api-key`.

> **`hail login` is managed-cloud only.** It runs the auth backend's device flow against `hail-website` and writes the resulting `hl_live_*` key to `~/.hail/credentials.json`. In self-host there's no website to authorize against — set `HAIL_API_KEY` directly and you're done.

#### Managed cloud

Run `hail login`. The CLI opens `/device` on the website, you approve, and it exchanges the device-flow session for a long-lived `hl_live_*` key minted by the auth backend into the `apikey` table. `hail/api` reads the same table — keys minted in the console work everywhere (CLI, MCP, direct API calls).

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

| symptom                                                                                    | root cause + fix                                                                                                                                                               |
| ------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `voicebot` container exits showing the typer help menu                                     | CMD missing the `start` subcommand. Fixed in the Dockerfile; if you fork it, keep `["python", "-m", "hailhq.voicebot.main", "start"]`.                                         |
| `ModuleNotFoundError: No module named 'hailhq'` after a Docker build                       | Hatchling wheel config used `packages = ["hailhq/<service>"]` — strips the `hailhq/` prefix. Must be `packages = ["hailhq"]`.                                                  |
| `exec /opt/venv/bin/uvicorn: no such file or directory`                                    | Renaming `/app/.venv` → `/opt/venv` between builder and runtime breaks shebangs. Keep the same path in both stages (we use `/app/.venv`).                                      |
| `RuntimeError: no running event loop` from `aiohttp.ClientSession()`                       | A FastAPI dep that constructs LiveKit/aiohttp must be `async def`. Sync deps run in a threadpool worker thread with no loop.                                                   |
| `google.auth.exceptions.DefaultCredentialsError: File  was not found.`                     | `GOOGLE_GENAI_USE_VERTEXAI=true` with empty `GOOGLE_APPLICATION_CREDENTIALS`. Default is `false`; opt into Vertex by flipping it AND providing creds.                          |
| `failed to parse tag 'cli-v0.1.0' as semver` from GoReleaser                               | OSS GoReleaser doesn't handle tag prefixes. The workflow's `Compute GoReleaser current/previous tags` step + `--skip=validate` flag handle it; don't remove either.            |
| `hailhq-core` references a workspace member, but is not one                                | Docker build context lacks the repo-root pyproject. The Dockerfile writes a minimal `/app/pyproject.toml` (workspace stub) inline before `uv sync`.                            |
| `pytest` from repo root fails with `ImportPathMismatchError`                               | Run each suite from its own directory. CI does this; replicate locally.                                                                                                        |
| First `pip install hail-sdk` from a venv with `hailhq.*` already installed shadows imports | The SDK is standalone by design. If you mix it with internal packages in the same venv, the `hail` package wins for `import hail` (intended); don't co-install for production. |

## Carry-forwards / open work

(See `CHANGELOG.md` "Deferred to v1.x" for the canonical list.)

- LiveKit Egress recording → S3 (currently `recording.py` returns `None`)
- `idempotency_keys` GC sweeper
- Inbound calls (`LIVEKIT_SIP_INBOUND_TRUNK_ID` reserved)
- SMS, Email channels
- `hail bootstrap` admin CLI (closes the manual DB-seed step above)
- CallEvent dedupe across voicebot redispatch
