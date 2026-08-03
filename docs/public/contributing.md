# Contributing

For the full operational runbook (releases, deployment, DB switching, all the known problems), refer to [Operations](./operations.md). This page covers the contribution flow only.

## Setup

```bash
git clone <repo>
cd hail
cp .env.example .env.local
# fill in keys (see ./setup/)
pnpm install                      # installs husky + lint-staged + prettier
docker compose -f docker-compose.yml -f docker-compose.local.yml up postgres minio
                                  # just the data services for host-side dev
```

`pnpm install` installs the git pre-commit hook. The hook runs `ruff`/`black`/`gofmt`/`prettier` on staged files.

## Dev loops

- API: `cd api && uv run uvicorn hailhq.api.main:app --reload --port 8080`
- Voicebot: `cd voicebot && uv run python -m hailhq.voicebot.main start`
- MCP: `cd mcp && uv run uvicorn hailhq.mcp.server:app --reload --port 8081`
- CLI: `cd cli && go run . <args>`

Full stack in Docker:

- Bundled Postgres: `docker compose -f docker-compose.yml -f docker-compose.local.yml up`
- Managed Postgres (set `DATABASE_URL` to your hosted URL first): `docker compose up`

## Database migrations

The schema lives in [`api/migrations/versions/`](https://github.com/hail-hq/hail/tree/main/api/migrations/versions). The Alembic config is in [`api/alembic.ini`](https://github.com/hail-hq/hail/blob/main/api/alembic.ini); `DATABASE_URL` overrides the config default.

```bash
cd api
uv run alembic upgrade head       # apply all pending
uv run alembic revision -m "add foo"   # create a new revision (hand-edit the SQL)
uv run alembic downgrade -1       # revert the last revision
```

Migrations are hand-written raw SQL for v1 (no ORM models yet). When SQLAlchemy models are released, switch to `--autogenerate`.

## Regenerating openapi.yaml

After you change API routes, dump the spec:

```bash
curl -s http://localhost:8080/openapi.json \
  | python -c "import json, sys, yaml; yaml.safe_dump(json.load(sys.stdin), sys.stdout, sort_keys=False)" \
  > openapi/openapi.yaml
```

The Go CLI codegens its client from this file, so commit the update in the same PR as the route change.

Do not hand-edit `openapi/openapi.yaml`. CI regenerates it from the live app
and compares (refer to `.github/workflows/openapi-check.yml`). Any manual
change that the app does not also produce fails as "stale". A status raised
via `raise HTTPException(...)` does **not** appear in the spec unless the
route decorator declares it (for example `responses={429: {...}}`). Add it
there, then regenerate.

## Commit style

[Conventional Commits](https://www.conventionalcommits.org):

- `feat(api): add POST /calls`
- `fix(voicebot): handle SIP disconnect during greeting`
- `docs(setup): clarify Twilio trunk origination URI`

## Adding a provider

Put new adapters under `core/hailhq/core/providers/<channel>/<name>.py`. Each adapter implements that channel's adapter interface. Add config keys to `.env.example` in the same provider-grouped format.

## Model costs contributions

Public AI model costs live in [`costs/`](https://github.com/hail-hq/hail/tree/main/costs) under CC-BY-4.0. The JSON files at the top of that directory are the source of truth. CI validates them against the schemas in `costs/schema/` on every PR.

To update a price:

1. Edit `costs/<category>.json` (for example `costs/llm.json`).
2. Set `last_verified` to today (`YYYY-MM-DD`). Set `verified_by` to your GitHub handle.
3. Update `source_url` if it has changed.
4. Run `pnpm costs:validate` locally before you push.

A weekly cron opens a tracking issue that lists rows older than 30 days — refer to [`costs-stale.yml`](https://github.com/hail-hq/hail/blob/main/.github/workflows/costs-stale.yml).

## What we will not merge (v1)

- Code that hard-codes a provider in `api/` or `voicebot/` — route through `core/`.
- New env vars missing from `.env.example`.
- Features without a milestone in README.
- Web UI code (no dashboards in v1).
- Docs that paraphrase the OpenAPI spec or MCP tool schemas instead of a link to the canonical source.
- Non-GFM Markdown in docs.
