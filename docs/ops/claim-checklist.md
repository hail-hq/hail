# Public-name claim checklist

Names and endpoints that should be locked down before v0.1.0. Ordered by squat-risk and dependency order. Items marked **(you)** require a human to click through a web UI; items marked **(repo)** are already scaffolded in code.

## 1. GitHub

- [x] **(you)** Create the `hail-hq` GitHub organization.
- [x] **(you)** Create the `hail-hq/hail` repository (public) and push `main`.
  - Unblocks the `github.com/hail-hq/hail/cli` Go module path (already in `cli/go.mod`).
  - Unblocks `ghcr.io/hail-hq/*` for future Docker images.
- [x] **(you)** Create the `hail-hq/homebrew-tap` repository (public).
  - Enables `brew install hail-hq/tap/hail` once a Formula is added (later milestone).

## 2. PyPI — `hail-sdk`

The primary external Python artifact. Scaffolded at [`sdk/`](../../sdk/) as a v0.0.1 placeholder.

- [x] **(you)** Create a PyPI account (or use an org-owned one) and enable 2FA.
- [x] **(you)** Configure a **Pending Trusted Publisher** at <https://pypi.org/manage/account/publishing/>.
- [x] **(repo)** Tag `sdk-v0.0.1` and push. [`release-sdk.yml`](../../.github/workflows/release-sdk.yml) publishes on `sdk-v*` tags via OIDC — `hail-sdk 0.0.1` lives at <https://pypi.org/project/hail-sdk/>.
- [ ] **(you)** Promote the Pending publisher to a normal Trusted Publisher attached to the now-existing `hail-sdk` project.

### Other PyPI names (lower priority)

Internal package names (`hailhq-core`, `hailhq-api`, `hailhq-voicebot`, `hailhq-mcp`) all carry `Private :: Do Not Upload` and are not published. Squat-risk is low (names have no external signal). Reserve them only if you want belt-and-braces — same procedure as `hail-sdk` with empty placeholders.

## 3. npm — `@hail-hq/` scope

- [x] **(you)** Create `hail-hq` organization on <https://www.npmjs.com/>.
- [ ] No package to publish yet (JS/TS SDK isn't in v1). Scope reservation is sufficient.
- [ ] Future: when a JS SDK lands, publish as `@hail-hq/sdk` via a similar `release-sdk-js.yml` workflow.

## 4. Docker Hub (optional)

GHCR (`ghcr.io/hail-hq/*`) is attached to the GitHub org and is the primary image registry. Docker Hub is optional for discoverability.

- [ ] **(you)** Create `hailhq` organization on <https://hub.docker.com/> if desired.

## 5. DNS / domain

`hail.so` — reportedly owned. Plan for subdomains:

- `hail.so` — marketing / landing
- `docs.hail.so` — published docs (fumadoc)
- `api.hail.so` — Hail Cloud API
- `mcp.hail.so/sse` — Hail Cloud MCP endpoint
- `app.hail.so` — dashboard (post-v1)

## 6. MCP server directories

Once the MCP server is live (service, not placeholder):

- [ ] Submit PR to [`modelcontextprotocol/servers`](https://github.com/modelcontextprotocol/servers) listing Hail under "Third-party servers".
- [ ] Add entry to any community-maintained "Awesome MCP" lists.

## 7. Social (lowest priority)

- Twitter / X handle: `@hail_hq` or `@hail_so`.
- Discord / community channel: later.

## Release-tag conventions

Per-component tag prefixes so multiple artifacts can ship independently:

- `sdk-v0.0.1`, `sdk-v0.1.0`, … → triggers `release-sdk.yml`.
- `cli-v0.1.0`, … → triggers `release-cli.yml` (future, via GoReleaser).
- `v0.1.0` (no prefix) → overall repo release; cut when v1 M1 is done.
