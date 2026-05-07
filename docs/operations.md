# Operations runbook

Single source of truth for how to develop, deploy, migrate, and release Hail. If you're an AI agent picking up the codebase, **read this first** alongside `CLAUDE.md`.

## Quick reference

| task                           | command                                                                              |
| ------------------------------ | ------------------------------------------------------------------------------------ |
| Bring up self-host stack       | `docker compose up -d`                                                               |
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
docker compose up -d                                       # postgres + minio + api + voicebot + mcp
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

### First-run DB seed (manual)

> Hail Cloud users skip this — run `hail login` instead, which provisions an API key against the managed API at `https://api.hail.so` and writes it to `~/.hail/credentials.json`.

`hail bootstrap` admin CLI is on the v1.x roadmap. Until then, self-hosters seed manually:

```bash
# 1) Generate an API key + its hash + 8-char prefix; export to env
read -r HAIL_API_KEY _KEY_PREFIX _KEY_HASH < <(python3 -c 'import base64,hashlib,secrets;r=secrets.token_bytes(32);k="hk_"+base64.urlsafe_b64encode(r).rstrip(b"=").decode();print(k,k[:8],hashlib.sha256(k.encode()).hexdigest())') && export HAIL_API_KEY _KEY_PREFIX _KEY_HASH && echo "$HAIL_API_KEY"

# 2) Set Twilio number details + API URL
export TWILIO_E164='+1XXXXXXXXXX' TWILIO_PN_SID='PNxxxxxxxxxxxxxxxx' HAIL_API_URL='http://localhost:8080'

# 3) Insert org + api_key + phone_number
docker compose exec -T postgres psql -U hail -d hail -c "INSERT INTO organizations (name, slug) VALUES ('Dev', 'dev');"
docker compose exec -T postgres psql -U hail -d hail -c "INSERT INTO api_keys (organization_id, name, key_prefix, key_hash) SELECT id, 'dev-key', '${_KEY_PREFIX}', '${_KEY_HASH}' FROM organizations WHERE slug = 'dev';"
docker compose exec -T postgres psql -U hail -d hail -c "INSERT INTO phone_numbers (organization_id, e164, country_code, number_type, capabilities, provider, provider_resource_id, provisioning_state, acquired_at) SELECT id, '${TWILIO_E164}', 'US', 'local', ARRAY['voice','sms'], 'twilio', '${TWILIO_PN_SID}', 'active', now() FROM organizations WHERE slug = 'dev';"
```

Save `HAIL_API_KEY` somewhere durable — it's only shown once (only the SHA-256 is stored).

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

### Switching the database

**Reset the local docker postgres** (lose all data, fresh start):

```bash
docker compose down -v                                     # -v removes volumes
docker compose up -d postgres
docker compose run --rm api alembic upgrade head
# re-seed (Phase above)
docker compose up -d
```

**Switch to managed (Neon / Supabase / RDS)**:

```bash
# 1. Provision the DB; get a postgresql+psycopg://... URL
# 2. Update DATABASE_URL in .env (and .env.local for host-side dev)
# 3. Apply migrations
docker compose run --rm api alembic upgrade head
# 4. Re-seed (same SQL as above; replace `docker compose exec -T postgres
#    psql -U hail -d hail -c` with `psql "$DATABASE_URL" -c`)
# 5. Optional: drop `postgres` service from docker-compose.yml + the
#    `depends_on: postgres` lines in api/voicebot/mcp services
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
