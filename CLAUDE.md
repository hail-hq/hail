# CLAUDE.md

Notes for Claude (and other AI assistants) working in this repo.

## What Hail is

A universal communication platform for AI agents. Outbound phone calls in v1; SMS, email, inbound to follow. Self-hostable via Docker Compose. Consumed via OpenAPI, CLI (`hail`), and an MCP server. AGPLv3.

## Repo layout

```
api/        — FastAPI service. Deployed (Docker), not published.
voicebot/   — LiveKit Agents worker service. Deployed (Docker), not published.
mcp/        — MCP server. Publishes `hail-mcp` on PyPI.
core/       — shared Python lib. Publishes `hail-core` on PyPI.
cli/        — Go binary (`hail`). Released as a binary; codegens its client from `openapi/openapi.yaml`.
openapi/    — committed openapi.yaml. Source of truth for the CLI.
docs/       — plain markdown; to be served via fumadoc on the website.
```

Only `hail-core` and `hail-mcp` are PyPI packages. `api/` and `voicebot/` have `pyproject.toml` for the uv workspace and Docker builds, but carry `Private :: Do Not Upload` to prevent accidental publish.

All four Python packages share the `hail` namespace (PEP 420 implicit namespace packages). Do **not** create `hail/__init__.py` at the namespace root.

Go CLI module path is `github.com/hail-hq/hail/cli`. npm packages are published under the `@hail-hq/` scope.

## Tenets

1. **Clear comms.** Explicit OpenAPI contracts. No hidden behavior.
2. **Simple code.** Boring is best. No abstractions without two concrete uses.
3. **Brief docs.** Each doc fits on one screen. Setup ≤ 10 minutes from a fresh clone.
4. **Self-hostable.** `docker compose up` runs everything except LiveKit Cloud.
5. **Pluggable brain.** BYO endpoint compatible with OpenAI's completions API, or use Hail's bundled fallback (OpenAI → Gemini → Anthropic). Voice pipeline + transport are always Hail's.

## Invariants

- **OpenAPI is source of truth for the CLI.** After any API route change, regenerate `openapi/openapi.yaml` in the same PR.
- **Secrets live only in `.env` / `.env.local`.** Only `.env.example` is committed. Adding a new env var? Update `.env.example` in the same commit, under the right provider section.
- **Provider adapters go in `core/hail/core/providers/<channel>/<name>.py`.** `api/` and `voicebot/` must not import provider SDKs directly; they go through `core`.
- **Shared models go in `core/`.** No duplicated Call/SMS/Email schemas across services.
- **AGPLv3.** Any derived SaaS must release source. Be conservative about copying third-party code.

## Dev commands

- Data services:  `docker compose up postgres minio`
- API:            `cd api && uv run uvicorn hail.api.main:app --reload --port 8080`
- Voicebot:       `cd voicebot && uv run python -m hail.voicebot.main`
- MCP:            `cd mcp && uv run python -m hail.mcp.server`
- CLI:            `cd cli && go run . <cmd>`
- Full stack:     `docker compose up`

## Style

- **Python**: ruff (format + lint), mypy, pytest. FastAPI async handlers. Type-hinted. Pydantic v2 models.
- **Go**: `gofmt`, stdlib first. Cobra for subcommands if/when the CLI grows them.
- **Docker**: multi-stage (builder → runtime), runs as non-root `hail` user, runtime image carries no build tools. Deps installed into `/opt/venv`; only that is copied to the runtime stage. Tighten to a pinned `uv.lock`-based cache flow once a lockfile lands.
- **Commits**: Conventional Commits.

## Do not

- Commit `.env` or `.env.local`.
- Introduce a dashboard / web UI in v1.
- Add a dependency without checking license compatibility with AGPLv3.
- Add env vars without updating `.env.example` in the same commit.
- Duplicate schemas across services (use `core/`).
- Run destructive git operations unprompted.
