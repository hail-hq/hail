# CLI DX pass — design

Status: draft
Owners: r13i

## Goals & non-goals

**Goal.** Treat `hail` as a daily-driver CLI. Close the obvious gaps between today's command tree and what a user expects to find, make unauthenticated state the first thing they see, and remove the one subcommand subtree that does not belong on the CLI.

**Non-goals (this milestone).**

- Multiple auth profiles. One profile, one creds file. Future-additive.
- `GET /me`, `hail auth status`, `hail whoami`. Single profile makes `whoami` redundant; `hail login` already prints what matters at sign-in time, and the root help line shows the signed-in identity.
- `hail call cancel`. Blocked on a `POST /calls/{id}/cancel` endpoint that does not exist; out of scope here.
- `hail open` (console-in-browser). Cut for simplicity.
- `hail email domain edit` and `hail email domain rotate-webhook-secret`. Cut for simplicity; PATCH and rotate still ship in the API.
- Cross-channel verbs (`hail send`, `hail status <id>`, `hail get <type>:<id>`). Channel-first grouping stays.
- `--format` templating, `--watch` shortcuts, JWT auth mode in the CLI, dashboard / web UI.

## 1. Tenets

These three rules apply to every command, new and existing.

1. **Unauthenticated UX is first-class.** When no creds are present (no `~/.hail/credentials.json`, no `HAIL_API_KEY`, no `--api-key`), the very first thing the user sees is the instruction to run `hail login`. This holds whether they invoke `hail`, `hail --help`, or any auth-requiring subcommand.
2. **Help on missing inputs, not blind errors.** Every command that requires positionals or specific flags prints `cmd.Help()` and exits **2** when validation fails. No bare cobra `Error: required flag(s) "X" not set` lines.
3. **Channel-first grouping.** Verbs hang off `call`, `email`, `tail`, `auth`, `mcp`. No top-level cross-channel sugar.

## 2. Command tree (target shape)

Legend: **bold** = new in this design pass · ~~strike~~ = removed by this design pass · everything else already ships.

```
hail
├── login                                       # top-level shortcut → `hail auth login`
├── auth
│   ├── login
│   ├── logout                                  # NEW — idempotent
│   └── token                                   # NEW — prints bare API key for scripting
├── call <to-number>
│   ├── status <id>        [alias: get]
│   ├── list               [alias: ls]
│   └── tail <id>                               # NEW — sugar → `hail tail call:<uuid>`
├── email
│   ├── send
│   ├── list               [alias: ls]
│   ├── get <id>
│   ├── tail <id>                               # NEW — sugar → `hail tail email:<uuid>`
│   ├── raw <id>                                # NEW — RFC 5322 to stdout (or --output)
│   ├── attachment <id> <attachment-id>         # NEW — binary to stdout (or --output)
│   └── domain
│       ├── register
│       ├── list           [alias: ls]
│       ├── get <id>
│       ├── verify <id>
│       └── delete         [alias: rm]
├── tail                                        # CHANGED — accepts `--id <type>:<uuid>` AND positional `<type>:<uuid>`; types: call, email
├── ~~webhooks~~                                # REMOVED in this milestone
├── mcp
│   └── endpoint                                # NEW — prints Streamable HTTP URL for current API
├── completion bash|zsh|fish                    # NEW — cobra-generated shell completions
└── version                                     # NEW — same string as `hail --version`
```

## 3. Auth subtree

### `hail auth login`

Unchanged. Top-level `hail login` stays as a shortcut. Long help mentions `auth logout` and `auth token` as siblings.

### `hail auth logout`

Delete `~/.hail/credentials.json`. Idempotent — already-gone is success and prints `Already signed out.`. Does **not** revoke server-side (no API endpoint exposed). When `DELETE /api-keys/{id}` lands, add `--revoke`.

### `hail auth token`

Print the bare API key to stdout, nothing else. Use:

```
export HAIL_API_KEY=$(hail auth token)
```

No `--json` (defeats the point). Exits **2** with the standard unauthenticated message if no creds.

## 4. Unauthenticated UX

Three call sites share one message constant.

### Root help (`hail` with no args / `hail --help`)

Long text built dynamically in `NewRootCmd` via `SetHelpFunc`, branching on whether the resolved `Options` carry an `APIKey`:

```
hail — universal communication platform for AI agents.

{{if not authenticated}}
Get started:
  hail login          Authenticate with Hail
{{else}}
Signed in as <prefix>•…<suffix> → <api-url>
{{end}}

Common:
  hail call +1...     Place an outbound call
  hail email send …   Send an email
  hail tail           Stream events across the org

More:
  hail --help         Full command list
```

The signed-in line is computed locally from `~/.hail/credentials.json` (key prefix/suffix + `api_url`) — no `GET /me` call, no server round-trip on `hail --help`.

### Any auth-requiring subcommand without creds

A new helper replaces the inline `errors.New("missing API key…")` in `(*Options).newClient` (`root.go:146`):

```go
func requireAuth(opts *Options) error {
    if opts.APIKey != "" { return nil }
    return errNotAuthenticated
}
```

`Execute()` recognizes `errNotAuthenticated`, prints the shared message to stderr, exits **2**:

```
hail: not authenticated.

  Run `hail login` to authenticate, or set HAIL_API_KEY / pass --api-key.
```

### Subcommands that tolerate no auth

`login`, `auth login`, `auth logout`, `version`, `completion`, `mcp endpoint`. Everything else gates through `requireAuth`.

## 5. Help on missing inputs

### Shared helper

One general helper covers three input-validation failure modes: missing required field, mutually-exclusive conflict, and rejected flag. The helper prints a one-line reason, then `cmd.Help()`, then returns a sentinel error that `Execute()` recognizes.

```go
var errInvalidInputs = errors.New("invalid inputs")

// helpAndFail prints `reason` + the command's full help to stderr and returns
// errInvalidInputs. Execute() recognizes the error type, skips re-printing,
// and exits 2.
func helpAndFail(cmd *cobra.Command, reason string) error {
    fmt.Fprintf(cmd.ErrOrStderr(), "hail: %s\n\n", reason)
    _ = cmd.Help()
    return errInvalidInputs
}

// requireInputs is the missing-required convenience wrapper.
func requireInputs(cmd *cobra.Command, missing ...string) error {
    if len(missing) == 0 { return nil }
    return helpAndFail(cmd, "missing required: "+strings.Join(missing, ", "))
}

func argsOrHelp(n int, want string) cobra.PositionalArgs {
    return func(cmd *cobra.Command, args []string) error {
        if len(args) != n { return requireInputs(cmd, want) }
        return nil
    }
}
```

`Execute()` in `root.go`:

```go
if errors.Is(err, errInvalidInputs) || errors.Is(err, errNotAuthenticated) {
    os.Exit(2)
}
```

### Where the tenet lands in existing code

| Current pattern                                                                            | Replace with                                                                                                      |
| ------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------- |
| `cmd.MarkFlagRequired("subject")` in `email.go:88`, `webhooks.go:67`, `email_domain.go:91` | Drop the `MarkFlagRequired` call; check at the top of `RunE` and call `requireInputs(cmd, "--subject")` if unset. |
| `Args: cobra.ExactArgs(1)`                                                                 | `Args: argsOrHelp(1, "<call-id>")` (or whatever the positional means).                                            |
| `validateMode` in `call.go:123` returning bare `errors.New(...)`                           | Same flow: call `helpAndFail(cmd, "<one-line mismatch reason>")` from `RunE` after the check fails.               |

### Auth gate ≠ missing-inputs gate

`requireAuth` does not print `cmd.Help()` — the user knows what they wanted; they need creds, not a flag list. Separate error type, same exit code.

## 6. Tail, channel-shaped tail sugar, inbound binary fetchers

### `hail tail` extension

Today `supportedResourceTypes` in `tail.go:50` is `{"call"}`. Extend to `{"call", "email"}` and:

- Accept positional `<type>:<uuid>` alongside `--id <type>:<uuid>`. If both supplied, they must agree; otherwise `helpAndFail(cmd, "--id and positional disagree")` and exit 2.
- Auto-exit for `email` resource type: terminal email statuses come from `core/schemas.py` (verify the `EmailStatus` enum before coding — likely `sent`, `failed`, `delivered`, `bounced`). Add a `terminalEmailStatuses` map mirroring `terminalCallStatuses`; dispatch on `resourceType`.
- Rename `singleCall` to `singleResource` in `renderEvent` so per-event short-id prefixing is suppressed for any single-resource tail.
- New inbound-email event kinds render via the existing default branch in `renderEventBody`; add friendlier labels only if the default JSON dump reads poorly.

### `hail call tail <id>` and `hail email tail <id>`

Thin wrappers. Each:

1. Resolves the id via the existing `resolveCallID` / `resolveEmailID` in `resolve.go` (full UUID or 4+ char prefix).
2. Re-uses `runTail` with `f.id = "<type>:<full-uuid>"`.

Help text points at `hail tail` for the canonical flags (`--from-start`, `--no-follow`, `--interval`, `--kind`).

### `hail email raw <id>` → `GET /emails/{email_id}/raw`

- Stdout by default; `--output path.eml` writes to disk; `--output -` is the same as omitting.
- Server content-type `message/rfc822`; CLI emits raw bytes, no JSON wrapping.
- `--json` is unsupported; rejected with `helpAndFail(cmd, "--json is not supported on raw — output is a binary stream")` and full help.

### `hail email attachment <id> <attachment-id>` → `GET /emails/{email_id}/attachments/{attachment_id}`

- Same I/O contract as `raw`: stdout default, `--output path` to a file.
- When stdout is a TTY and `--output` is not set: refuse, print "binary stream; pass --output or pipe to a file". Same UX guard `curl` uses.
- `attachment-id` resolution: fetched from the parent `GET /emails/{id}` body (small) so prefix-matching works within that email's attachment list. No new resolver helper for a child resource with no list endpoint.

## 7. Discoverability commands

### `hail version`

```
$ hail version
hail 0.5.0 (commit a1b2c3, built 2026-06-28)
```

Same `version` / `commit` / `buildDate` ldflags `root.go` already exposes via `--version`. `--json` returns `{ "version": "...", "commit": "...", "built": "..." }`.

### `hail completion bash|zsh|fish`

One subcommand that dispatches to cobra's built-in generators (`GenBashCompletionV2`, `GenZshCompletion`, `GenFishCompletion`). Validates against the supported set; anything else exits 2 with help. Long help shows the install snippet per shell.

### `hail mcp endpoint`

Print the Streamable HTTP URL for the MCP service fronting the current API. Convention-based derivation from `Options.ResolvedAPIURL()` — cloud convention is `api.hail.so` → `mcp.hail.so/mcp`; verify against `docs/setup/mcp.md` before coding. Self-host: prints the value of an env var or sensible default.

Tolerates no auth. `--json` returns `{"url": "...", "transport": "streamable-http"}`.

## 8. Rollout

All new surfaces ship in this milestone: `auth logout`, `auth token`, `call tail`, `email tail`, `email raw`, `email attachment`, extended `hail tail`, `mcp endpoint`, `completion`, `version`. Unauthenticated UX (tenet 1) and help-on-missing (tenet 2) apply to every command, new and existing.

`hail webhooks` is **removed outright** in this milestone — no deprecation grace period. Files deleted: `cli/internal/cmd/webhooks.go`, `cli/internal/cmd/webhooks_test.go`, plus the `resolveWebhookID` and `resolveWebhookDeliveryID` helpers in `resolve.go`. The generated client calls remain (webhooks remain on the API); only the CLI consumer goes. Anyone still scripting `hail webhooks …` gets `Error: unknown command "webhooks"`. Self-hosters debug delivery state via SQL on the webhook\_\* tables; cloud users manage subscriptions from the console.

No other renames in this pass.

## 9. Tests

The existing `runRoot(t, stdout, stderr, env, args...)` helper invokes the cobra tree with injected IO; use it everywhere.

### Per new command (minimum)

| Command                                 | Cases                                                                                                                                                                                               |
| --------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `auth logout`                           | (a) no creds → "Already signed out." exit 0. (b) creds present → file removed, exit 0.                                                                                                              |
| `auth token`                            | (a) no creds → unauth gate, exit 2, no stdout. (b) creds → prints key, nothing else, exit 0.                                                                                                        |
| `call tail <id>`                        | (a) full UUID → delegates to `runTail` with `f.id = "call:<uuid>"`. (b) prefix → resolves then delegates. (c) bad prefix → resolver error, no tail output.                                          |
| `email tail <id>`                       | mirror of `call tail`.                                                                                                                                                                              |
| `email raw <id>`                        | (a) stdout: raw bytes match the fake server's body. (b) `--output path.eml`: file matches. (c) `--json` → exit 2 with help.                                                                         |
| `email attachment <id> <attachment-id>` | (a) writes to stdout, (b) `--output`, (c) refuses to write binary to TTY (mock the TTY check), (d) prefix resolution within parent email's attachment list.                                         |
| `tail` extension                        | (a) positional `call:<uuid>` round-trips to existing renderer. (b) positional `email:<uuid>` round-trips. (c) positional and `--id` conflict → help + exit 2. (d) unsupported type → help + exit 2. |
| `mcp endpoint`                          | (a) text form, (b) `--json` form, (c) no creds tolerated.                                                                                                                                           |
| `completion`                            | one case per supported shell asserting the first line matches each generator's signature.                                                                                                           |
| `version`                               | (a) text form matches `--version`, (b) `--json`.                                                                                                                                                    |

### Cross-cutting gates

- **Help-on-missing**: one test per command that requires positionals or flags, asserting (i) exit 2, (ii) stderr contains `missing required:`, (iii) stderr contains a substring of `cmd.Long` (proving `Help()` ran).
- **Unauth gate**: one test that invokes any auth-requiring command with no creds and asserts the standard message + exit 2.
- **Deprecation banner**: one test per `hail webhooks` subcommand asserting the deprecation line appears in stderr exactly once.

## 10. Docs

- Update `README.md`'s CLI snippet to reflect the new tree (one screen).
- Touch the `hail auth …` paragraph in `docs/operations.md` (self-host bootstrap).
- No new doc files; the repo tenet says each doc fits on one screen.

The `openapi.yaml` is not touched in this design (no API changes). No migration in `api/migrations/`.

## 11. Out of scope (re-stated)

- `hail auth status / whoami` and `GET /me`.
- `hail call cancel` (needs API endpoint).
- `hail open`.
- `hail webhooks` re-design (just removed; if it comes back later, it is a fresh spec).
- `hail email domain edit / rotate-webhook-secret`.
- Multiple profiles, JWT mode, `--format` templating, `--watch` shortcuts.
