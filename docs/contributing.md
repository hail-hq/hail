# Contributing

## Setup

```bash
git clone <repo>
cd hail
cp .env.example .env.local
# fill in keys (see docs/setup/*)
docker compose up postgres minio  # just the data services for host-side dev
```

## Dev loops

- API:      `cd api && uv run uvicorn hail.api.main:app --reload --port 8080`
- Voicebot: `cd voicebot && uv run python -m hail.voicebot.main`
- MCP:      `cd mcp && uv run python -m hail.mcp.server`
- CLI:      `cd cli && go run . <args>`

Full stack in Docker: `docker compose up`.

## Regenerating openapi.yaml

After changing API routes, dump the spec:

```bash
curl -s http://localhost:8080/openapi.json \
  | python -c "import json, sys, yaml; yaml.safe_dump(json.load(sys.stdin), sys.stdout, sort_keys=False)" \
  > openapi/openapi.yaml
```

The Go CLI codegens its client from this file, so commit the update in the same PR as the route change.

## Commit style

[Conventional Commits](https://www.conventionalcommits.org):

- `feat(api): add POST /calls`
- `fix(voicebot): handle SIP disconnect during greeting`
- `docs(setup): clarify Twilio trunk origination URI`

## Adding a provider

New adapters live under `core/hail/core/providers/<channel>/<name>.py` and implement that channel's adapter interface. Add config keys to `.env.example` using the same provider-grouped format.

## What we won't merge (v1)

- Code that hard-codes a provider in `api/` or `voicebot/` — route through `core/`.
- New env vars missing from `.env.example`.
- Features without a milestone in README.
- Web UI code (no dashboards in v1).
