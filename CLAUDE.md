# CLAUDE.md

Notes for Claude (and other AI assistants) working in this repo.

## What Hail is

Give your AI agent a voice, a real phone number, and an inbox. Calls in and out, SMS in and out, email in and out. Saved agents answer calls and texts on numbers (docs/public/agents.md). Self-hostable via Docker Compose. Consumed via OpenAPI, CLI (`hail`), and an MCP server. AGPLv3.

## Repo layout

```
api/        — FastAPI service. Deployed (Docker), not published.
voicebot/   — LiveKit Agents worker service. Deployed (Docker), not published.
mcp/        — MCP service, remote Streamable HTTP transport on :8081. Deployed (Docker), not published.
core/       — shared Python lib. Not published to PyPI in v1 (no external consumers yet).
cli/        — Go binary (`hail`). Released via GitHub Releases; codegens its client from `openapi/openapi.yaml`.
openapi/    — committed openapi.yaml. Source of truth for the CLI.
docs/       — plain markdown prose (architecture, contributing, operations, setup/*); GitHub renders it.
web/        — Next.js workspace for the interactive costs surface (/costs, /costs/compare, /costs.md, /costs/schema/*). Deployed to Vercel; hail-website rewrites /costs/* to it.
```

**PyPI posture for v1**: one external artifact — the Python SDK, published as `hail-sdk` (imports as `import hail`). The internal services `hailhq-api`, `hailhq-voicebot`, `hailhq-mcp` all carry `Private :: Do Not Upload`. `hailhq-core` is not marked private but is not released in v1 either — it's an internal workspace dep.

**MCP distribution**: remote Streamable HTTP only. We deliberately do **not** ship a stdio PyPI package. Reasoning lives in [docs/public/mcp.md](docs/public/mcp.md) — read it before proposing a stdio server; the default answer is "no, use the remote HTTP endpoint".

Internal packages share the `hailhq.*` namespace (PEP 420 implicit — **no** `hailhq/__init__.py` at the namespace root). The import name `hail` is **reserved** for the external SDK (`hail-sdk` on PyPI); do not introduce top-level `hail` modules inside the monorepo.

Go CLI module path is `github.com/hail-hq/hail/cli`. npm packages are published under the `@hail-hq/` scope.

## Tenets

1. **Clear comms.** Explicit OpenAPI contracts. No hidden behavior.
2. **Simple code.** Boring is best. No abstractions without two concrete uses.
3. **Brief docs.** Each doc fits on one screen. Setup ≤ 10 minutes from a fresh clone.
4. **Self-hostable.** `docker compose up` runs everything except LiveKit Cloud.
5. **Pluggable brain.** BYO endpoint compatible with OpenAI's completions API, or use Hail's bundled fallback (OpenAI → Gemini → Anthropic). Voice pipeline + transport are always Hail's.
6. **Agent-first docs.** AI agents are first-class readers. Lead with concrete, runnable examples; link to canonical sources (OpenAPI spec, MCP tool schemas, code paths) rather than paraphrase them. Every page should let a reader — human or agent — take the next action.

## Invariants

- **OpenAPI is source of truth for the CLI.** After any API route change, regenerate `openapi/openapi.yaml` in the same PR.
- **Secrets live only in `.env` / `.env.local`.** Only `.env.example` is committed. Adding a new env var? Update `.env.example` in the same commit, under the right provider section.
- **`.env` mirrors `.env.example` line for line.** Same keys, same order, same comments; only the values differ. A branch that adds keys writes them, with real values, into the main checkout `.env` so it is deployment-ready at merge. Never add a key or comment to `.env` that its `.env.example` does not have.
- **Provider adapters go in `core/hailhq/core/providers/<channel>/<name>.py`.** `api/` and `voicebot/` must not import provider SDKs directly; they go through `core`.
- **Webhook events have one registry.** Add event names, labels, and channels in `core/hailhq/core/webhook_events.py`. The public `/v1/webhooks/event-types` catalog and subscription validation use this registry. Do not add a console event allowlist.
- **Shared models go in `core/`.** No duplicated Call/SMS/Email schemas across services.
- **AGPLv3.** Any derived SaaS must release source. Be conservative about copying third-party code.
- **Docs are agent-first.** When writing or updating any doc: lead with a concrete runnable example, link canonical sources (OpenAPI spec, MCP tool schemas, code paths) instead of paraphrasing them, and avoid screenshots when a snippet would work. Use GitHub-flavored Markdown.
- **URLs are not strings.** Use [`hailhq.core.urls`](core/hailhq/core/urls.py) helpers (`canonical_url`, `url_variants`, `join_url`) — never f-string `{base}/{path}`, never raw `==` against a URL minted by another service, never `.rstrip("/")` ad-hoc. Pydantic adds trailing slashes to root URLs; Node TS doesn't; OAuth audience checks are exact-string equality. Skipping this has cost us OAuth flows already (consent succeeded, token endpoint rejected `resource=https://mcp.hail.so/` because hail-website held `https://mcp.hail.so`). If a URL crosses a language boundary and lands in any comparison, reach for the helpers.

## Schema changes (tables and columns)

Group data by what it is, not by the feature that needs it. No approval step: follow these rules every time.

1. **Reuse first.** Before any migration, list the tables that already hold the same kind of record. Read `core/hailhq/core/models.py` (hail API tables) and `better-auth_migrations/*.sql` (auth tables in hail-website).
2. **Extend the closest table.** A new variant of an existing record gets a `reason` or `kind` column on that table, not a sibling table. Example: idle-number warnings are rows in `number_dunning` with `reason = 'idle'`, next to the low-balance rows (`reason = 'balance'`).
3. **Keep entity tables lean.** Do not add one feature's job state (warn dates, retry counters) as columns on `phone_numbers`, `calls`, `sms`, `emails`, `organizations`, or `users` when a state table for that job exists or fits.
4. **New table only when nothing fits.** That means no existing table holds that kind of record. The migration docstring names the tables you checked and why each does not fit.
5. **Uniqueness and links in the database.** Use FKs with an explicit `ondelete`, CHECK constraints for allowed values, and partial unique indexes when one table holds more than one kind of row.
6. **Remove what you stop using.** A PR that stops using a table or column also drops it, or opens a follow-up issue that names it.

## Dev commands

- Data services (local Postgres): `docker compose -f docker-compose.yml -f docker-compose.local.yml up postgres`
- Migrations: `cd api && uv run alembic upgrade head`
- API: `cd api && uv run uvicorn hailhq.api.main:app --reload --port 8080`
- Voicebot: `cd voicebot && uv run python -m hailhq.voicebot.main start`
- MCP: `cd mcp && uv run uvicorn hailhq.mcp.server:app --reload --port 8081`
- CLI: `cd cli && go run . <cmd>`
- Full stack (bundled Postgres): `docker compose -f docker-compose.yml -f docker-compose.local.yml up`
- Full stack (managed Postgres via `DATABASE_URL`): `docker compose up`

The base `docker-compose.yml` assumes `DATABASE_URL` reaches a Postgres you bring; `docker-compose.local.yml` is a thin overlay that adds the local `postgres` container and wires `depends_on` for `api` and `voicebot`.

For the comprehensive operations runbook (releases, deployment, DB switching,
known footguns), see [docs/public/self-host/operations.md](docs/public/self-host/operations.md). **AI agents
picking up this codebase: read it before making any infra-shaped change.**

Internal runbooks (not published) live in `docs/operations/`:
[carrier-verification.md](docs/operations/carrier-verification.md) (verification states, background pass, cancel rules),
[number-catalog-sync.md](docs/operations/number-catalog-sync.md) (the `costs/<carrier>.json` files, weekly sync, secrets, purchase gate),
[refresh-costs.md](docs/operations/refresh-costs.md) (weekly LLM/STT/TTS price refresh),
[didww-by-request.md](docs/operations/didww-by-request.md) (ordering a DIDWW number a customer asked support for).

## Style

- **Python**: ruff (lint, `--fix`) + black (format); ruff, black and pytest run in CI. FastAPI async handlers. Type-hinted. Pydantic v2 models.
- **Go**: `gofmt`, stdlib first. Cobra for subcommands if/when the CLI grows them.
- **Docker**: multi-stage (builder → runtime), runs as non-root `hail` user, runtime image carries no build tools. Deps installed into `/opt/venv`; only that is copied to the runtime stage. Tighten to a pinned `uv.lock`-based cache flow once a lockfile lands.
- **Pre-commit**: husky + lint-staged run `ruff`/`black`/`gofmt`/`prettier` on staged files. Install with `pnpm install` at repo root; hooks activate automatically via the `prepare` script. pnpm is the Node package manager for this repo (pinned via `packageManager` in `package.json`).
- **Commits**: Conventional Commits.

## Do not

- Commit `.env` or `.env.local`.
- Introduce a dashboard / web UI in v1.
- Add a dependency without checking license compatibility with AGPLv3.
- Add env vars without updating `.env.example` in the same commit.
- Duplicate schemas across services (use `core/`).
- Run destructive git operations unprompted.
