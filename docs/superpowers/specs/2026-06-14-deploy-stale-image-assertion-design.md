# Post-rollout stale-image assertion — design

**Date:** 2026-06-14
**Status:** draft, pending review

## Problem

`deploy.yml` builds three images (`api`, `voicebot`, `mcp`), pushes them to
GHCR, then SSHes the VM to `docker compose pull && up -d`. Nothing verifies
that the containers now running were actually built from the commit being
deployed. If a `pull` silently serves a cached layer, a registry hiccup leaves
`:latest` stale, or one service fails to recreate, the deploy reports success
while an old build keeps running. We hit a version of this confusion manually
(the MCP appeared to lack new tools); the deploy itself was fine, but we had no
automated signal either way.

The original ask was a "tool-count assertion" on the MCP. That is a weak proxy:
it is MCP-only, the expected count is a magic number that drifts, and reading
the deployed tool list requires an OAuth bearer (the endpoint 401s otherwise).
We want a check that covers all three services and directly answers "is the
running container the image built from this commit?"

## Goal

Fail the deploy if any of `api` / `voicebot` / `mcp` is not running the image
built from `$DEPLOY_GITHUB_SHA`, immediately after `up -d`.

## Approach A — image-digest assertion (recommended)

The build job already tags every image with both `:latest` and
`type=sha,format=long` → `sha-<full-sha>` (`deploy.yml:65-67`). Both tags point
at the **same image ID** within a build. So, on the VM after `up -d`, for each
service compare the running container's image ID against the image ID of the
`sha-<deployed>` tag:

```sh
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml"
assert_fresh() {
  svc="$1"
  cid="$($COMPOSE ps -q "$svc")"
  [ -n "$cid" ] || { echo "::error::$svc has no running container"; exit 1; }
  running="$(docker inspect --format '{{.Image}}' "$cid")"
  expected="$(docker image inspect --format '{{.Id}}' \
    "ghcr.io/hail-hq/hail-$svc:sha-$DEPLOY_GITHUB_SHA")"
  if [ "$running" != "$expected" ]; then
    echo "::error::$svc is stale — running $running, expected $expected (sha-$DEPLOY_GITHUB_SHA)"
    exit 1
  fi
  echo "$svc: fresh ($expected)"
}
for svc in api voicebot mcp; do assert_fresh "$svc"; done
```

Insert this block in the SSH `script:` between `$COMPOSE up -d --remove-orphans`
and the final `$COMPOSE ps` (`deploy.yml:124-125`). `set -euo pipefail` is
already in effect, so a non-zero exit fails the deploy step.

Why this is the right altitude:

- **Zero app/Dockerfile changes.** Nothing to build, nothing to keep in sync.
- **Covers all three services**, including the HTTP-less `voicebot` worker.
- **Directly verifies** container-image provenance, not a hand-maintained proxy.

Two failure modes that could undermine the check are already closed by the
existing workflow, so they need no extra handling here: `concurrency:
deploy-prod` (`deploy.yml:32-34`) serializes deploys, so `:latest` and
`:sha-<sha>` from one build can't diverge under a racing deploy; and the
`fail-fast` matrix + `needs: build` (`deploy.yml:45,81`) means no partial image
set ever reaches the deploy job.

- The `sha-<sha>` tag is pulled by `$COMPOSE pull` only if referenced; it is
  not (compose hard-codes `:latest`). So the script must pull the SHA tag
  explicitly first, OR inspect the local `:latest` digest instead. See "Open
  decision" below.

### Open decision (A)

`docker image inspect ghcr.io/...:sha-<sha>` requires that tag to exist locally.
Two ways to guarantee it:

1. **Pull the SHA tag** for each service before asserting:
   `docker pull -q "ghcr.io/hail-hq/hail-$svc:sha-$DEPLOY_GITHUB_SHA"`. One
   extra pull per service (cheap — same digest as `:latest`, fully cached).
2. **Compare against `:latest` instead** and separately assert that the local
   `:latest` was freshly pulled. Weaker (doesn't tie to the commit), so prefer
   option 1.

Recommendation: option 1 — explicit SHA-tag pull, then digest compare.

## Approach B — `/healthz` build-SHA (complementary, optional)

Add the build SHA to the runtime so humans and uptime monitors can read what is
deployed at any time (not just at deploy):

- `deploy.yml` build step: pass `build-args: GIT_SHA=${{ github.sha }}`.
- Each `Dockerfile`: `ARG GIT_SHA` → `ENV HAIL_BUILD_SHA=${GIT_SHA}` in the
  runtime stage.
- `api/main.py` `healthz()` and `mcp/server.py` `healthz()` return
  `{"status": "ok", "sha": os.environ.get("HAIL_BUILD_SHA", "")}`.

This does **not** replace Approach A as the deploy gate:

- `voicebot` has no HTTP server, so `/healthz` cannot cover it.
- It adds a build-arg + env to three Dockerfiles and two routes to keep in sync.

Its value is ongoing observability (curl `/healthz`, see the version), not the
rollout assertion. Ship it only if that observability is wanted; the deploy gate
is Approach A alone.

## Recommendation

Implement **Approach A** (image-digest assertion, option 1) as the deploy gate.
Treat **Approach B** as a separate, optional observability follow-up.

## Out of scope

- The MCP tool-count check (superseded by A — see Problem).
- Rollback automation on a failed assertion (the deploy simply fails red; the
  prior `:latest` keeps running because `up -d` recreates in place — operator
  investigates per the runbook).

## Env / invariants

- No new operator-set env var. `HAIL_BUILD_SHA` (Approach B only) is image-baked
  at build time, not set via `.env`, so `.env.example` is unaffected.
- This is an infra-shaped change to `deploy.yml`; see
  [docs/operations.md](../../operations.md) → deployment section before
  applying.

## Testing & verification

- **A:** dry-run the assert block on the VM against the current deploy — it must
  print `fresh` for all three. Then force a negative: re-tag an old image as a
  fake `sha-…` locally and confirm the script exits non-zero.
- **B (if shipped):** `curl -s localhost:8080/healthz` and `:8081/healthz`
  return the deployed `sha`; add an assertion to the existing
  `mcp`/`api` health unit tests that the key is present.
