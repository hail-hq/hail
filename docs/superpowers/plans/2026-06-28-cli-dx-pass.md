# CLI DX pass — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the gaps in the `hail` CLI — auth logout/token, tail sugar, inbound binary fetchers, mcp endpoint, completion, version — while removing `hail webhooks` and lifting unauthenticated UX and help-on-missing into cross-command tenets.

**Architecture:** Two cross-cutting helpers (`helpAndFail`/`requireInputs` for input validation, `requireAuth` for credentials) replace ad-hoc cobra patterns. New subcommands hang off the existing channel-first tree. Auth UX surfaces a clear login hint at every entry point. Existing `hail webhooks` ships a deprecation banner + `Hidden:true` in this milestone; the actual deletion lands in 0.7.x in a separate change.

**Tech Stack:** Go 1.24, cobra, oapi-codegen-generated client at `cli/internal/client/client.gen.go`, `httptest`-based test harness at `cli/internal/cmd/call_test.go:73` (`runRoot(t, env, args...)`).

**Spec:** `docs/superpowers/specs/2026-06-28-cli-dx-pass-design.md`

---

## File map

### New files

- `cli/internal/cmd/helpfail.go` — `helpAndFail`, `requireInputs`, `argsOrHelp`, `errInvalidInputs`. One import boundary for the help-on-missing tenet.
- `cli/internal/cmd/helpfail_test.go`
- `cli/internal/cmd/version.go` — `hail version` subcommand.
- `cli/internal/cmd/version_test.go`
- `cli/internal/cmd/completion.go` — `hail completion bash|zsh|fish`.
- `cli/internal/cmd/completion_test.go`
- `cli/internal/cmd/mcp.go` — `hail mcp endpoint`.
- `cli/internal/cmd/mcp_test.go`
- `cli/internal/cmd/auth_logout.go` — `hail auth logout`.
- `cli/internal/cmd/auth_logout_test.go`
- `cli/internal/cmd/auth_token.go` — `hail auth token`.
- `cli/internal/cmd/auth_token_test.go`
- `cli/internal/cmd/call_tail.go` — `hail call tail <id>` sugar.
- `cli/internal/cmd/call_tail_test.go`
- `cli/internal/cmd/email_tail.go` — `hail email tail <id>` sugar.
- `cli/internal/cmd/email_tail_test.go`
- `cli/internal/cmd/email_raw.go` — `hail email raw <id>`.
- `cli/internal/cmd/email_raw_test.go`
- `cli/internal/cmd/email_attachment.go` — `hail email attachment <id> <attachment-id>`.
- `cli/internal/cmd/email_attachment_test.go`

### Modified files

- `cli/internal/cmd/root.go` — error routing in `Execute()`, `requireAuth` helper, `SetHelpFunc` for dynamic Long, registration of new commands.
- `cli/internal/cmd/login.go` — register `logout` and `token` under `hail auth`.
- `cli/internal/cmd/credentials.go` — add `deleteCredentials()` helper.
- `cli/internal/cmd/call.go` — `argsOrHelp` on the parent, `validateMode` uses `helpAndFail`.
- `cli/internal/cmd/call_status.go` — `argsOrHelp` on positional `<call-id>`.
- `cli/internal/cmd/email.go` — drop `MarkFlagRequired("subject")`, use `requireInputs`. Register new email subcommands (`tail`, `raw`, `attachment`).
- `cli/internal/cmd/email_domain.go` — drop `MarkFlagRequired("kind")`, use `requireInputs`. `argsOrHelp` on the id-taking subcommands.
- `cli/internal/cmd/webhooks.go` — deprecation banner + `Hidden:true` per subcommand.
- `cli/internal/cmd/tail.go` — extend `supportedResourceTypes`, accept positional, add `terminalEmailStatuses`, rename `singleCall` → `singleResource`.
- `cli/internal/cmd/resolve.go` — no changes (`resolveCallID`/`resolveEmailID` already exist).
- `README.md` and `docs/operations.md` — reflect the new command tree.

---

## Task 1: Shared `helpAndFail` / `requireInputs` / `argsOrHelp` helper

**Files:**

- Create: `cli/internal/cmd/helpfail.go`
- Create: `cli/internal/cmd/helpfail_test.go`

- [ ] **Step 1: Write the failing test**

```go
// cli/internal/cmd/helpfail_test.go
package cmd

import (
	"bytes"
	"errors"
	"strings"
	"testing"

	"github.com/spf13/cobra"
)

func TestHelpAndFail_PrintsReasonAndHelp(t *testing.T) {
	cmd := &cobra.Command{Use: "demo", Long: "demo-long-text"}
	var out bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&out)

	err := helpAndFail(cmd, "bad inputs")
	if !errors.Is(err, errInvalidInputs) {
		t.Fatalf("want errInvalidInputs, got %v", err)
	}
	got := out.String()
	if !strings.Contains(got, "hail: bad inputs") {
		t.Fatalf("missing reason: %q", got)
	}
	if !strings.Contains(got, "demo-long-text") {
		t.Fatalf("missing Help() output: %q", got)
	}
}

func TestRequireInputs_NoMissingIsNil(t *testing.T) {
	cmd := &cobra.Command{Use: "demo"}
	if err := requireInputs(cmd); err != nil {
		t.Fatalf("want nil, got %v", err)
	}
}

func TestRequireInputs_FormatsList(t *testing.T) {
	cmd := &cobra.Command{Use: "demo", Long: "demo-long"}
	var out bytes.Buffer
	cmd.SetErr(&out)
	cmd.SetOut(&out)

	err := requireInputs(cmd, "--a", "--b")
	if !errors.Is(err, errInvalidInputs) {
		t.Fatalf("want errInvalidInputs, got %v", err)
	}
	if !strings.Contains(out.String(), "missing required: --a, --b") {
		t.Fatalf("missing formatted list: %q", out.String())
	}
}

func TestArgsOrHelp_MismatchPrintsHelp(t *testing.T) {
	cmd := &cobra.Command{Use: "demo", Long: "demo-long"}
	var out bytes.Buffer
	cmd.SetErr(&out)
	cmd.SetOut(&out)

	err := argsOrHelp(1, "<id>")(cmd, []string{})
	if !errors.Is(err, errInvalidInputs) {
		t.Fatalf("want errInvalidInputs, got %v", err)
	}
	if !strings.Contains(out.String(), "missing required: <id>") {
		t.Fatalf("missing arg name: %q", out.String())
	}
}

func TestArgsOrHelp_MatchReturnsNil(t *testing.T) {
	cmd := &cobra.Command{Use: "demo"}
	if err := argsOrHelp(1, "<id>")(cmd, []string{"x"}); err != nil {
		t.Fatalf("want nil, got %v", err)
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd cli && go test ./internal/cmd -run Help -v`
Expected: FAIL — `helpAndFail`, `requireInputs`, `argsOrHelp`, `errInvalidInputs` undefined.

- [ ] **Step 3: Write the helper file**

```go
// cli/internal/cmd/helpfail.go
package cmd

import (
	"errors"
	"fmt"
	"strings"

	"github.com/spf13/cobra"
)

// errInvalidInputs is returned by helpAndFail / requireInputs / argsOrHelp.
// Execute() in root.go converts it to exit code 2 without re-printing —
// the helpers have already written the reason + Help() to stderr.
var errInvalidInputs = errors.New("invalid inputs")

// helpAndFail prints a single-line reason to stderr followed by the
// command's full help, then returns errInvalidInputs. Use it whenever an
// input validation fails — missing fields, mutually-exclusive conflicts,
// rejected flags. The unified flow keeps "no blind error messages" easy
// to honor across the whole CLI.
func helpAndFail(cmd *cobra.Command, reason string) error {
	fmt.Fprintf(cmd.ErrOrStderr(), "hail: %s\n\n", reason)
	_ = cmd.Help()
	return errInvalidInputs
}

// requireInputs is the missing-required convenience wrapper. Variadic so
// callers can list every absent field in a single call:
//
//	if err := requireInputs(cmd, "--subject", "--body"); err != nil { return err }
func requireInputs(cmd *cobra.Command, missing ...string) error {
	if len(missing) == 0 {
		return nil
	}
	return helpAndFail(cmd, "missing required: "+strings.Join(missing, ", "))
}

// argsOrHelp builds a PositionalArgs validator that calls requireInputs
// with `want` when the arg count is wrong. Replaces cobra.ExactArgs at
// each command where we want the full help dumped on mismatch.
func argsOrHelp(n int, want string) cobra.PositionalArgs {
	return func(cmd *cobra.Command, args []string) error {
		if len(args) != n {
			return requireInputs(cmd, want)
		}
		return nil
	}
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd cli && go test ./internal/cmd -run Help -v`
Expected: PASS (5 cases).

- [ ] **Step 5: Commit**

```bash
git add cli/internal/cmd/helpfail.go cli/internal/cmd/helpfail_test.go
git commit -m "feat(cli): add helpAndFail/requireInputs/argsOrHelp helpers"
```

---

## Task 2: Route `errInvalidInputs` and `errNotAuthenticated` in `Execute()`; add `requireAuth`

**Files:**

- Modify: `cli/internal/cmd/root.go`

- [ ] **Step 1: Write the failing test**

Add to `cli/internal/cmd/helpfail_test.go`:

```go
import (
	"os/exec"
	"runtime"
)

// We can't observe os.Exit from within the process — covered in
// integration tests below. Instead, verify the error sentinel is
// recognized: invoking a parser that returns errInvalidInputs must
// propagate it out of Execute()'s callers (NewRootCmd path).
func TestRootCmd_InvalidInputsBubblesUp(t *testing.T) {
	root := NewRootCmd(&bytes.Buffer{}, &bytes.Buffer{}, func(string) string { return "" })
	root.SetArgs([]string{"call"}) // missing positional <to-number>
	err := root.Execute()
	if !errors.Is(err, errInvalidInputs) {
		t.Fatalf("want errInvalidInputs, got %v", err)
	}
}

func TestRequireAuth_NoKeyReturnsErrNotAuthenticated(t *testing.T) {
	opts := &Options{}
	err := requireAuth(opts)
	if !errors.Is(err, errNotAuthenticated) {
		t.Fatalf("want errNotAuthenticated, got %v", err)
	}
}

func TestRequireAuth_WithKeyReturnsNil(t *testing.T) {
	opts := &Options{APIKey: "sk_test"}
	if err := requireAuth(opts); err != nil {
		t.Fatalf("want nil, got %v", err)
	}
}

// Silence unused import warnings when not all branches use them.
var _ = runtime.GOOS
var _ = exec.Command
```

The `TestRootCmd_InvalidInputsBubblesUp` test depends on `call`'s positional check being wired in Task 4. For Task 2 the focus is the route+helper; we'll re-run it after Task 4. The other two cases pass now.

- [ ] **Step 2: Run tests to verify the two new cases fail (no `requireAuth` / `errNotAuthenticated` yet)**

Run: `cd cli && go test ./internal/cmd -run RequireAuth -v`
Expected: FAIL — undefined.

- [ ] **Step 3: Add `errNotAuthenticated`, `requireAuth`, and route both error types**

Edit `cli/internal/cmd/root.go`. At the top of the file (after existing `var` block), add:

```go
// errNotAuthenticated is returned by requireAuth when the resolved
// Options carry no API key from any source (--api-key, $HAIL_API_KEY,
// or credentials file). Execute() recognizes it and prints the canonical
// "run hail login" hint before exiting 2.
var errNotAuthenticated = errors.New("not authenticated")

// requireAuth gates auth-requiring subcommands. Call it at the top of
// RunE before any I/O; commands that tolerate no auth (login, version,
// completion, mcp endpoint) skip it entirely.
func requireAuth(opts *Options) error {
	if opts.APIKey != "" {
		return nil
	}
	return errNotAuthenticated
}
```

Replace `Execute()` with:

```go
// Execute parses os.Args and runs the root command. It is the binary entry
// point and the only place that calls os.Exit, so subcommand handlers can
// remain pure (return error, propagate up).
//
// Exit-code policy:
//   0  — success
//   2  — input validation failed (errInvalidInputs) or unauthenticated
//        (errNotAuthenticated). Distinct from a generic failure so scripts
//        can branch on "user error" vs "everything else".
//   130 — SIGINT (errInterrupted), POSIX convention.
//   1  — generic error.
func Execute() {
	root := NewRootCmd(os.Stdout, os.Stderr, os.Getenv)
	if err := root.Execute(); err != nil {
		switch {
		case errors.Is(err, errInterrupted):
			os.Exit(130)
		case errors.Is(err, errInvalidInputs):
			// Reason + Help() were already printed by helpAndFail. Exit silently.
			os.Exit(2)
		case errors.Is(err, errNotAuthenticated):
			fmt.Fprintln(os.Stderr, "hail: not authenticated.")
			fmt.Fprintln(os.Stderr)
			fmt.Fprintln(os.Stderr, "  Run `hail login` to authenticate, or set HAIL_API_KEY / pass --api-key.")
			os.Exit(2)
		default:
			fmt.Fprintln(os.Stderr, "hail:", err)
			os.Exit(1)
		}
	}
}
```

Update `(*Options).newClient` (in `cli/internal/cmd/root.go` around line 144) — replace the inline error with `requireAuth`:

```go
func (o *Options) newClient(extra ...client.RequestEditorFn) (*client.ClientWithResponses, error) {
	if err := requireAuth(o); err != nil {
		return nil, err
	}
	editors := append([]client.RequestEditorFn{authEditor(o.APIKey)}, extra...)
	clientOpts := make([]client.ClientOption, len(editors))
	for i, e := range editors {
		clientOpts[i] = client.WithRequestEditorFn(e)
	}
	c, err := client.NewClientWithResponses(o.ResolvedAPIURL(), clientOpts...)
	if err != nil {
		return nil, fmt.Errorf("client init: %w", err)
	}
	return c, nil
}
```

- [ ] **Step 4: Run tests**

Run: `cd cli && go test ./internal/cmd -run RequireAuth -v`
Expected: PASS (both cases).

Run: `cd cli && go test ./internal/cmd/... -v` and assert no regressions in the existing suite. Existing tests that asserted on `"missing API key"` substring need updating — search and adjust:

```bash
cd cli && grep -nE "missing API key" internal/cmd/*_test.go
```

If any matches, replace the assertion with `errors.Is(err, errNotAuthenticated)` (no message-string coupling).

- [ ] **Step 5: Commit**

```bash
git add cli/internal/cmd/root.go cli/internal/cmd/helpfail_test.go
git commit -m "feat(cli): route errInvalidInputs/errNotAuthenticated; add requireAuth"
```

---

## Task 3: Replace `MarkFlagRequired` + `cobra.ExactArgs` on existing commands

**Files:**

- Modify: `cli/internal/cmd/call.go`
- Modify: `cli/internal/cmd/call_status.go`
- Modify: `cli/internal/cmd/email.go`
- Modify: `cli/internal/cmd/email_domain.go`
- Modify: existing `*_test.go` that asserted on cobra's default error strings.

- [ ] **Step 1: Write failing tests for the help-on-missing behavior**

Add to `cli/internal/cmd/email_test.go`:

```go
func TestEmailSend_MissingSubject_PrintsHelpAndFails(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, map[string]any{})
	_, stderr, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "send", "--to", "a@b.com", "--body", "hi",
	)
	if !errors.Is(err, errInvalidInputs) {
		t.Fatalf("want errInvalidInputs, got %v", err)
	}
	if !strings.Contains(stderr, "missing required: --subject") {
		t.Fatalf("missing reason line: %q", stderr)
	}
	if !strings.Contains(stderr, "hail email send") {
		t.Fatalf("expected Help() output in stderr: %q", stderr)
	}
}
```

Add to `cli/internal/cmd/call_test.go`:

```go
func TestCallSubcommand_MissingPositional_PrintsHelp(t *testing.T) {
	_, stderr, err := runRoot(t, map[string]string{"HAIL_API_KEY": "sk_test"}, "call")
	if !errors.Is(err, errInvalidInputs) {
		t.Fatalf("want errInvalidInputs, got %v", err)
	}
	if !strings.Contains(stderr, "missing required: <to-number>") {
		t.Fatalf("missing arg name: %q", stderr)
	}
}
```

- [ ] **Step 2: Run tests to confirm failure**

Run: `cd cli && go test ./internal/cmd -run "TestEmailSend_MissingSubject|TestCallSubcommand_MissingPositional" -v`
Expected: FAIL — current behavior prints cobra's default `Error: required flag(s) "subject" not set` or short usage, not the spec'd format.

- [ ] **Step 3: Apply `argsOrHelp` and `requireInputs` to existing commands**

`cli/internal/cmd/call.go` — at the `newCallCmd` `Args:` line, change:

```go
Args: cobra.ExactArgs(1),
```

to:

```go
Args: argsOrHelp(1, "<to-number>"),
```

In the same file, replace `validateMode` so the bare `errors.New` calls go through `helpAndFail`. Change:

```go
func validateMode(f *callFlags) error {
	hasPrompt := f.prompt != ""
	hasAnyLLM := f.llmURL != "" || f.llmKey != "" || f.llmModel != ""
	hasFullLLM := f.llmURL != "" && f.llmKey != "" && f.llmModel != ""

	if hasPrompt && hasAnyLLM {
		return errors.New("--prompt and --llm-* are mutually exclusive (use one mode)")
	}
	if !hasPrompt && !hasAnyLLM {
		return errors.New("must provide either --prompt or all of --llm-url --llm-key --llm-model")
	}
	if hasAnyLLM && !hasFullLLM {
		return errors.New("--llm-url, --llm-key, and --llm-model must all be supplied together")
	}
	return nil
}
```

to take the `*cobra.Command` and route through `helpAndFail`:

```go
func validateMode(cmd *cobra.Command, f *callFlags) error {
	hasPrompt := f.prompt != ""
	hasAnyLLM := f.llmURL != "" || f.llmKey != "" || f.llmModel != ""
	hasFullLLM := f.llmURL != "" && f.llmKey != "" && f.llmModel != ""

	if hasPrompt && hasAnyLLM {
		return helpAndFail(cmd, "--prompt and --llm-* are mutually exclusive (use one mode)")
	}
	if !hasPrompt && !hasAnyLLM {
		return helpAndFail(cmd, "must provide either --prompt or all of --llm-url --llm-key --llm-model")
	}
	if hasAnyLLM && !hasFullLLM {
		return helpAndFail(cmd, "--llm-url, --llm-key, and --llm-model must all be supplied together")
	}
	return nil
}
```

Update the caller in `runCall` to pass `cmd`. Edit `RunE` of the same `newCallCmd` and `runCall` signature:

```go
RunE: func(cmd *cobra.Command, args []string) error {
	return runCall(cmd, opts, f, args[0])
},
```

and:

```go
func runCall(cmd *cobra.Command, opts *Options, f *callFlags, toNumber string) error {
	if err := validateMode(cmd, f); err != nil {
		return err
	}
	// ... rest unchanged
}
```

Adjust existing `validateMode` tests in `call_test.go` to pass a non-nil `*cobra.Command` (use a dummy `&cobra.Command{}`).

`cli/internal/cmd/call_status.go` — change:

```go
Args: cobra.ExactArgs(1),
```

to:

```go
Args: argsOrHelp(1, "<call-id>"),
```

`cli/internal/cmd/email.go` — at `newEmailSendCmd`, delete the `MarkFlagRequired("subject")` block. Add a check at the top of `runEmailSend`:

```go
func runEmailSend(ctx context.Context, cmd *cobra.Command, opts *Options, f *emailSendFlags) error {
	if f.subject == "" {
		return requireInputs(cmd, "--subject")
	}
	// ... rest unchanged
}
```

Update the `RunE` accordingly:

```go
RunE: func(cmd *cobra.Command, _ []string) error {
	return runEmailSend(cmd.Context(), cmd, opts, f)
},
```

Make the matching change to existing `--body` / `--to` validation inside `runEmailSend` so those use `requireInputs` too:

```go
if len(to) == 0 {
	return requireInputs(cmd, "--to")
}
// ...
if bodyText == "" && bodyHTML == "" {
	return requireInputs(cmd, "--body or --body-html or --body-file or --body-html-file")
}
```

`cli/internal/cmd/email_domain.go` — in `newEmailDomainRegisterCmd`, drop `MarkFlagRequired("kind")`. Add the check at the top of `runEmailDomainRegister`:

```go
func runEmailDomainRegister(
	ctx context.Context, cmd *cobra.Command, opts *Options, f *emailDomainRegisterFlags,
) error {
	if f.kind == "" {
		return requireInputs(cmd, "--kind")
	}
	if f.kind != "hail_mail" && f.kind != "custom" {
		return helpAndFail(cmd, "--kind must be 'hail_mail' or 'custom'")
	}
	// ... rest unchanged
}
```

Plumb `cmd` through the `RunE` closure.

Change each id-taking subcommand (`get`, `verify`, `delete`) from `cobra.ExactArgs(1)` to `argsOrHelp(1, "<id>")`.

- [ ] **Step 4: Run the focused new tests, then the full suite**

Run: `cd cli && go test ./internal/cmd -run "TestEmailSend_MissingSubject|TestCallSubcommand_MissingPositional" -v`
Expected: PASS.

Run: `cd cli && go test ./internal/cmd/... -v`
Expected: PASS for all. Fix any old assertions that coupled to cobra's `"Error: required flag(s) "X" not set"` substring — replace with `errors.Is(err, errInvalidInputs)`.

- [ ] **Step 5: Commit**

```bash
git add cli/internal/cmd/call.go cli/internal/cmd/call_status.go cli/internal/cmd/email.go cli/internal/cmd/email_domain.go cli/internal/cmd/email_test.go cli/internal/cmd/call_test.go
git commit -m "feat(cli): help-on-missing tenet across call and email subtrees"
```

---

## Task 4: Root help — dynamic Long with authenticated branch

**Files:**

- Modify: `cli/internal/cmd/root.go`

- [ ] **Step 1: Write the failing test**

Add to `cli/internal/cmd/root_test.go` (create if absent):

```go
package cmd

import (
	"strings"
	"testing"
)

func TestRootHelp_NotAuthenticated_LeadsWithLoginHint(t *testing.T) {
	stdout, _, err := runRoot(t, map[string]string{}, "--help")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(stdout, "Get started:") {
		t.Fatalf("expected unauthenticated banner: %q", stdout)
	}
	if !strings.Contains(stdout, "hail login") {
		t.Fatalf("expected login hint: %q", stdout)
	}
	if strings.Contains(stdout, "Signed in as") {
		t.Fatalf("did not expect signed-in line: %q", stdout)
	}
}

func TestRootHelp_Authenticated_ShowsSignedInLine(t *testing.T) {
	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "hl_live_abcdefghijklmnop", "HAIL_API_URL": "https://api.hail.so"},
		"--help",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(stdout, "Signed in as hl_live_") {
		t.Fatalf("expected signed-in line: %q", stdout)
	}
	if !strings.Contains(stdout, "https://api.hail.so") {
		t.Fatalf("expected api url: %q", stdout)
	}
	if strings.Contains(stdout, "Get started:") {
		t.Fatalf("did not expect get-started block: %q", stdout)
	}
}
```

- [ ] **Step 2: Run to confirm failure**

Run: `cd cli && go test ./internal/cmd -run RootHelp -v`
Expected: FAIL — current Long is static and contains neither "Get started" nor "Signed in as".

- [ ] **Step 3: Implement dynamic Long via `SetHelpFunc`**

In `cli/internal/cmd/root.go`, after the `root := &cobra.Command{…}` block in `NewRootCmd` (and after `PersistentPreRunE` runs at help-render time, cobra already invokes pre-run for non-help args; for `--help` we resolve creds manually inside the help func). Add:

```go
// Dynamic Long: the banner branches on whether creds resolved to anything.
// Cobra does NOT run PersistentPreRunE before printing help, so we resolve
// the auth state inline here. Keeps the help render free of side effects
// beyond reading creds.
root.SetHelpFunc(func(cmd *cobra.Command, args []string) {
	// Only customize the root help; subcommand --help paths stay default.
	if cmd != root {
		cmd.Root().HelpFunc()(cmd, args)
		return
	}
	apiKey := opts.APIKey
	apiURL := opts.APIURL
	if apiKey == "" {
		apiKey = getenv("HAIL_API_KEY")
	}
	if apiURL == "" {
		apiURL = getenv("HAIL_API_URL")
	}
	if apiKey == "" || apiURL == "" {
		if creds, _ := loadCredentials(); creds != nil {
			if apiKey == "" {
				apiKey = creds.APIKey
			}
			if apiURL == "" {
				apiURL = creds.APIURL
			}
		}
	}
	if apiURL == "" {
		apiURL = DefaultAPIURL
	}

	out := cmd.OutOrStdout()
	fmt.Fprintln(out, "hail — universal communication platform for AI agents.")
	fmt.Fprintln(out)
	if apiKey == "" {
		fmt.Fprintln(out, "Get started:")
		fmt.Fprintln(out, "  hail login          Authenticate with Hail")
	} else {
		fmt.Fprintf(out, "Signed in as %s → %s\n", maskAPIKey(apiKey), apiURL)
	}
	fmt.Fprintln(out)
	fmt.Fprintln(out, "Common:")
	fmt.Fprintln(out, "  hail call +1...     Place an outbound call")
	fmt.Fprintln(out, "  hail email send …   Send an email")
	fmt.Fprintln(out, "  hail tail           Stream events across the org")
	fmt.Fprintln(out)
	fmt.Fprintln(out, "More:")
	fmt.Fprintln(out, "  hail --help         Full command list")
})
```

Add helper at the bottom of the file:

```go
// maskAPIKey returns the prefix-•••-last4 form used in help banners and
// logs. Conservative: if the input is too short to mask safely, returns
// "(set)" so we never leak a partial secret that would itself be useful.
func maskAPIKey(k string) string {
	if len(k) < 12 {
		return "(set)"
	}
	// hl_live_ prefix is 8 chars; preserve it + suffix.
	return k[:8] + "•…" + k[len(k)-4:]
}
```

- [ ] **Step 4: Run the targeted tests, then the full suite**

Run: `cd cli && go test ./internal/cmd -run RootHelp -v`
Expected: PASS.

Run: `cd cli && go test ./internal/cmd/... -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cli/internal/cmd/root.go cli/internal/cmd/root_test.go
git commit -m "feat(cli): dynamic root help banner with login/signed-in branch"
```

---

## Task 5: `hail version` subcommand

**Files:**

- Create: `cli/internal/cmd/version.go`
- Create: `cli/internal/cmd/version_test.go`
- Modify: `cli/internal/cmd/root.go` (register)

- [ ] **Step 1: Write the failing test**

```go
// cli/internal/cmd/version_test.go
package cmd

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestVersion_TextMatchesLongFlag(t *testing.T) {
	subStdout, _, err := runRoot(t, map[string]string{}, "version")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.HasPrefix(subStdout, "hail ") {
		t.Fatalf("expected 'hail X.Y.Z ...' line: %q", subStdout)
	}
	if !strings.Contains(subStdout, "(commit ") {
		t.Fatalf("expected commit detail: %q", subStdout)
	}
}

func TestVersion_JSONShape(t *testing.T) {
	stdout, _, err := runRoot(t, map[string]string{}, "version", "--json")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var v struct {
		Version string `json:"version"`
		Commit  string `json:"commit"`
		Built   string `json:"built"`
	}
	if err := json.Unmarshal([]byte(stdout), &v); err != nil {
		t.Fatalf("expected JSON, got %q (err: %v)", stdout, err)
	}
	if v.Version == "" || v.Commit == "" || v.Built == "" {
		t.Fatalf("expected all three fields populated: %+v", v)
	}
}
```

- [ ] **Step 2: Run to confirm failure**

Run: `cd cli && go test ./internal/cmd -run TestVersion -v`
Expected: FAIL — `version` subcommand undefined.

- [ ] **Step 3: Implement**

```go
// cli/internal/cmd/version.go
package cmd

import (
	"encoding/json"
	"fmt"

	"github.com/spf13/cobra"
)

func newVersionCmd(opts *Options) *cobra.Command {
	return &cobra.Command{
		Use:   "version",
		Short: "Print hail CLI version, commit, and build date",
		Long: `hail version — print the same string as hail --version.

JSON form (for scripting):
  hail version --json`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			if opts.JSON {
				return json.NewEncoder(opts.Stdout).Encode(map[string]string{
					"version": version,
					"commit":  commit,
					"built":   buildDate,
				})
			}
			fmt.Fprintf(opts.Stdout, "hail %s (commit %s, built %s)\n", version, commit, buildDate)
			return nil
		},
	}
}
```

Register in `cli/internal/cmd/root.go` inside `NewRootCmd`, alongside the other `root.AddCommand(...)` calls:

```go
root.AddCommand(newVersionCmd(opts))
```

- [ ] **Step 4: Run tests**

Run: `cd cli && go test ./internal/cmd -run TestVersion -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cli/internal/cmd/version.go cli/internal/cmd/version_test.go cli/internal/cmd/root.go
git commit -m "feat(cli): add `hail version` subcommand"
```

---

## Task 6: `hail completion bash|zsh|fish`

**Files:**

- Create: `cli/internal/cmd/completion.go`
- Create: `cli/internal/cmd/completion_test.go`
- Modify: `cli/internal/cmd/root.go` (register)

- [ ] **Step 1: Write the failing test**

```go
// cli/internal/cmd/completion_test.go
package cmd

import (
	"errors"
	"strings"
	"testing"
)

func TestCompletion_Bash(t *testing.T) {
	stdout, _, err := runRoot(t, map[string]string{}, "completion", "bash")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(stdout, "bash completion") && !strings.Contains(stdout, "complete -F") {
		t.Fatalf("expected bash completion preamble: %q", stdout[:min(200, len(stdout))])
	}
}

func TestCompletion_Zsh(t *testing.T) {
	stdout, _, err := runRoot(t, map[string]string{}, "completion", "zsh")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(stdout, "compdef") {
		t.Fatalf("expected zsh compdef directive: %q", stdout[:min(200, len(stdout))])
	}
}

func TestCompletion_Fish(t *testing.T) {
	stdout, _, err := runRoot(t, map[string]string{}, "completion", "fish")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(stdout, "complete -c hail") {
		t.Fatalf("expected fish completion: %q", stdout[:min(200, len(stdout))])
	}
}

func TestCompletion_UnsupportedShell(t *testing.T) {
	_, stderr, err := runRoot(t, map[string]string{}, "completion", "tcsh")
	if !errors.Is(err, errInvalidInputs) {
		t.Fatalf("want errInvalidInputs, got %v", err)
	}
	if !strings.Contains(stderr, "supported: bash, zsh, fish") {
		t.Fatalf("expected supported list: %q", stderr)
	}
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
```

- [ ] **Step 2: Run to confirm failure**

Run: `cd cli && go test ./internal/cmd -run TestCompletion -v`
Expected: FAIL — subcommand undefined.

- [ ] **Step 3: Implement**

```go
// cli/internal/cmd/completion.go
package cmd

import (
	"github.com/spf13/cobra"
)

func newCompletionCmd(opts *Options) *cobra.Command {
	cmd := &cobra.Command{
		Use:                   "completion <bash|zsh|fish>",
		Short:                 "Generate a shell completion script",
		DisableFlagsInUseLine: true,
		Long: `hail completion — emit a shell completion script.

Install (pick one):

  # bash (system-wide)
  hail completion bash | sudo tee /etc/bash_completion.d/hail

  # zsh (per-user)
  echo 'source <(hail completion zsh)' >> ~/.zshrc

  # fish (per-user)
  hail completion fish > ~/.config/fish/completions/hail.fish`,
		Args:      argsOrHelp(1, "<bash|zsh|fish>"),
		ValidArgs: []string{"bash", "zsh", "fish"},
		RunE: func(cmd *cobra.Command, args []string) error {
			switch args[0] {
			case "bash":
				return cmd.Root().GenBashCompletionV2(opts.Stdout, true)
			case "zsh":
				return cmd.Root().GenZshCompletion(opts.Stdout)
			case "fish":
				return cmd.Root().GenFishCompletion(opts.Stdout, true)
			default:
				return helpAndFail(cmd, "unsupported shell "+args[0]+"; supported: bash, zsh, fish")
			}
		},
	}
	return cmd
}
```

Register in `cli/internal/cmd/root.go`:

```go
root.AddCommand(newCompletionCmd(opts))
```

- [ ] **Step 4: Run tests**

Run: `cd cli && go test ./internal/cmd -run TestCompletion -v`
Expected: PASS (4 cases).

- [ ] **Step 5: Commit**

```bash
git add cli/internal/cmd/completion.go cli/internal/cmd/completion_test.go cli/internal/cmd/root.go
git commit -m "feat(cli): add `hail completion bash|zsh|fish`"
```

---

## Task 7: `hail mcp endpoint`

**Files:**

- Create: `cli/internal/cmd/mcp.go`
- Create: `cli/internal/cmd/mcp_test.go`
- Modify: `cli/internal/cmd/root.go` (register)

- [ ] **Step 1: Write the failing test**

```go
// cli/internal/cmd/mcp_test.go
package cmd

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestMcpEndpoint_TextForm_DerivesFromAPIURL(t *testing.T) {
	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_URL": "https://api.hail.so"},
		"mcp", "endpoint",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(stdout, "https://mcp.hail.so/mcp") {
		t.Fatalf("expected derived URL: %q", stdout)
	}
}

func TestMcpEndpoint_JSON(t *testing.T) {
	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_URL": "https://api.hail.so"},
		"mcp", "endpoint", "--json",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var v struct {
		URL       string `json:"url"`
		Transport string `json:"transport"`
	}
	if err := json.Unmarshal([]byte(stdout), &v); err != nil {
		t.Fatalf("expected JSON, got %q (err: %v)", stdout, err)
	}
	if v.URL != "https://mcp.hail.so/mcp" {
		t.Fatalf("url mismatch: %q", v.URL)
	}
	if v.Transport != "streamable-http" {
		t.Fatalf("transport mismatch: %q", v.Transport)
	}
}

func TestMcpEndpoint_NoCredsTolerated(t *testing.T) {
	_, _, err := runRoot(t, map[string]string{}, "mcp", "endpoint")
	if err != nil {
		t.Fatalf("expected no-auth tolerance, got %v", err)
	}
}

func TestMcpEndpoint_SelfHostFallback(t *testing.T) {
	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_URL": "http://localhost:8080", "HAIL_MCP_URL": "http://localhost:8081/mcp"},
		"mcp", "endpoint",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(stdout, "http://localhost:8081/mcp") {
		t.Fatalf("expected self-host override: %q", stdout)
	}
}
```

- [ ] **Step 2: Run to confirm failure**

Run: `cd cli && go test ./internal/cmd -run TestMcpEndpoint -v`
Expected: FAIL — subcommand undefined.

- [ ] **Step 3: Implement**

```go
// cli/internal/cmd/mcp.go
package cmd

import (
	"encoding/json"
	"fmt"
	"net/url"
	"strings"

	"github.com/spf13/cobra"
)

func newMcpCmd(opts *Options) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "mcp",
		Short: "MCP-related subcommands",
		Long: `hail mcp — MCP-related subcommands.

Subcommands:
  endpoint    Print the Streamable HTTP URL of the MCP server fronting
              the current API.`,
	}
	cmd.AddCommand(newMcpEndpointCmd(opts))
	return cmd
}

func newMcpEndpointCmd(opts *Options) *cobra.Command {
	return &cobra.Command{
		Use:   "endpoint",
		Short: "Print the MCP server's Streamable HTTP URL",
		Long: `hail mcp endpoint — print the MCP server URL.

Resolution order:
  $HAIL_MCP_URL                                 (explicit override)
  api.<host>.<tld> → mcp.<host>.<tld>/mcp       (cloud convention)
  <api-url>                                     (self-host fallback)`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			endpoint := resolveMcpEndpoint(opts)
			if opts.JSON {
				return json.NewEncoder(opts.Stdout).Encode(map[string]string{
					"url":       endpoint,
					"transport": "streamable-http",
				})
			}
			fmt.Fprintln(opts.Stdout, endpoint)
			return nil
		},
	}
}

// resolveMcpEndpoint derives the MCP URL from configured sources.
// Order: HAIL_MCP_URL > api.<rest> → mcp.<rest>/mcp > the API URL itself.
func resolveMcpEndpoint(opts *Options) string {
	if v := opts.Getenv("HAIL_MCP_URL"); v != "" {
		return v
	}
	apiURL := opts.ResolvedAPIURL()
	if u, err := url.Parse(apiURL); err == nil && strings.HasPrefix(u.Host, "api.") {
		u.Host = "mcp." + strings.TrimPrefix(u.Host, "api.")
		u.Path = "/mcp"
		return u.String()
	}
	// Self-host fallback when no override and the host isn't api.<...>.
	return apiURL
}
```

Register in `cli/internal/cmd/root.go`:

```go
root.AddCommand(newMcpCmd(opts))
```

Add `HAIL_MCP_URL` to `.env.example` under the appropriate section (per repo invariant: env vars must be added in the same commit):

```bash
cd /Users/r/playground/hail && grep -n "HAIL_MCP_URL\|^# MCP" .env.example | head
```

If `HAIL_MCP_URL` is not present, add it under the MCP-shaped section with a comment:

```
# Overrides the derived MCP endpoint URL. Self-host operators set this; for
# Hail Cloud the CLI derives `mcp.<host>.so/mcp` from $HAIL_API_URL.
HAIL_MCP_URL=
```

- [ ] **Step 4: Run tests**

Run: `cd cli && go test ./internal/cmd -run TestMcpEndpoint -v`
Expected: PASS (4 cases).

- [ ] **Step 5: Commit**

```bash
git add cli/internal/cmd/mcp.go cli/internal/cmd/mcp_test.go cli/internal/cmd/root.go .env.example
git commit -m "feat(cli): add `hail mcp endpoint` with HAIL_MCP_URL override"
```

---

## Task 8: `hail auth logout`

**Files:**

- Create: `cli/internal/cmd/auth_logout.go`
- Create: `cli/internal/cmd/auth_logout_test.go`
- Modify: `cli/internal/cmd/credentials.go` (add delete helper)
- Modify: `cli/internal/cmd/login.go` (register under `auth`)

- [ ] **Step 1: Write the failing test**

```go
// cli/internal/cmd/auth_logout_test.go
package cmd

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// withTempHome redirects $HOME for the duration of the test so the
// credentials file lives in a sandboxed tree. Uses t.Setenv (process-level)
// because loadCredentials reads HOME via os.UserHomeDir, not opts.Getenv.
func withTempHome(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("HOME", dir)
	return dir
}

func TestAuthLogout_NoCredsFile_PrintsAlreadySignedOut(t *testing.T) {
	withTempHome(t)
	stdout, _, err := runRoot(t, map[string]string{}, "auth", "logout")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(stdout, "Already signed out") {
		t.Fatalf("expected idempotent message: %q", stdout)
	}
}

func TestAuthLogout_RemovesFile(t *testing.T) {
	home := withTempHome(t)
	if _, err := saveCredentials(Credentials{APIKey: "sk_test", APIURL: "https://api.hail.so"}); err != nil {
		t.Fatalf("seed: %v", err)
	}
	path := filepath.Join(home, ".hail", "credentials.json")
	if _, err := os.Stat(path); err != nil {
		t.Fatalf("expected creds file present: %v", err)
	}

	stdout, _, err := runRoot(t, map[string]string{}, "auth", "logout")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(stdout, "Signed out") {
		t.Fatalf("expected confirmation: %q", stdout)
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatalf("expected creds file removed, got: %v", err)
	}
}
```

- [ ] **Step 2: Run to confirm failure**

Run: `cd cli && go test ./internal/cmd -run TestAuthLogout -v`
Expected: FAIL — subcommand undefined.

- [ ] **Step 3: Implement**

Add to `cli/internal/cmd/credentials.go`:

```go
// deleteCredentials removes the credentials file. Returns (false, nil)
// if the file was already absent (idempotent), (true, nil) if it was
// deleted, or (_, err) on any other I/O failure.
func deleteCredentials() (existed bool, err error) {
	p, err := credentialsPath()
	if err != nil {
		return false, err
	}
	if err := os.Remove(p); err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return false, nil
		}
		return false, fmt.Errorf("remove %s: %w", p, err)
	}
	return true, nil
}
```

Make sure the imports include `errors` and `fmt` (they already do; `os` too).

Create `cli/internal/cmd/auth_logout.go`:

```go
// cli/internal/cmd/auth_logout.go
package cmd

import (
	"fmt"

	"github.com/spf13/cobra"
)

func newAuthLogoutCmd(opts *Options) *cobra.Command {
	return &cobra.Command{
		Use:   "logout",
		Short: "Remove the local Hail credentials file",
		Long: `hail auth logout — delete ~/.hail/credentials.json.

Idempotent. Does NOT revoke the API key server-side; revoke from the
console if you need to invalidate the key everywhere.`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			existed, err := deleteCredentials()
			if err != nil {
				return err
			}
			if existed {
				fmt.Fprintln(opts.Stdout, "Signed out.")
			} else {
				fmt.Fprintln(opts.Stdout, "Already signed out.")
			}
			return nil
		},
	}
}
```

Register in `cli/internal/cmd/login.go`'s `newAuthCmd`, alongside the existing `auth.AddCommand(newLoginCmd(opts))`:

```go
auth.AddCommand(newLoginCmd(opts))
auth.AddCommand(newAuthLogoutCmd(opts))
```

- [ ] **Step 4: Run tests**

Run: `cd cli && go test ./internal/cmd -run TestAuthLogout -v`
Expected: PASS (both cases).

- [ ] **Step 5: Commit**

```bash
git add cli/internal/cmd/auth_logout.go cli/internal/cmd/auth_logout_test.go cli/internal/cmd/credentials.go cli/internal/cmd/login.go
git commit -m "feat(cli): add `hail auth logout`"
```

---

## Task 9: `hail auth token`

**Files:**

- Create: `cli/internal/cmd/auth_token.go`
- Create: `cli/internal/cmd/auth_token_test.go`
- Modify: `cli/internal/cmd/login.go` (register under `auth`)

- [ ] **Step 1: Write the failing test**

```go
// cli/internal/cmd/auth_token_test.go
package cmd

import (
	"errors"
	"strings"
	"testing"
)

func TestAuthToken_NoCreds_ReturnsErrNotAuthenticated(t *testing.T) {
	withTempHome(t)
	stdout, _, err := runRoot(t, map[string]string{}, "auth", "token")
	if !errors.Is(err, errNotAuthenticated) {
		t.Fatalf("want errNotAuthenticated, got %v", err)
	}
	if stdout != "" {
		t.Fatalf("expected empty stdout, got %q", stdout)
	}
}

func TestAuthToken_PrintsBareKey(t *testing.T) {
	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "hl_live_abcdef"},
		"auth", "token",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	got := strings.TrimRight(stdout, "\n")
	if got != "hl_live_abcdef" {
		t.Fatalf("want bare key only, got %q", stdout)
	}
}
```

- [ ] **Step 2: Run to confirm failure**

Run: `cd cli && go test ./internal/cmd -run TestAuthToken -v`
Expected: FAIL — subcommand undefined.

- [ ] **Step 3: Implement**

```go
// cli/internal/cmd/auth_token.go
package cmd

import (
	"fmt"

	"github.com/spf13/cobra"
)

func newAuthTokenCmd(opts *Options) *cobra.Command {
	return &cobra.Command{
		Use:   "token",
		Short: "Print the bare API key (for scripting)",
		Long: `hail auth token — print the resolved API key to stdout.

Use for shell scripting:

  export HAIL_API_KEY=$(hail auth token)

Resolves --api-key > $HAIL_API_KEY > ~/.hail/credentials.json. Exits 2
with the standard not-authenticated hint if no key is configured.`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			if err := requireAuth(opts); err != nil {
				return err
			}
			fmt.Fprintln(opts.Stdout, opts.APIKey)
			return nil
		},
	}
}
```

Register in `cli/internal/cmd/login.go`:

```go
auth.AddCommand(newLoginCmd(opts))
auth.AddCommand(newAuthLogoutCmd(opts))
auth.AddCommand(newAuthTokenCmd(opts))
```

- [ ] **Step 4: Run tests**

Run: `cd cli && go test ./internal/cmd -run TestAuthToken -v`
Expected: PASS (both cases).

- [ ] **Step 5: Commit**

```bash
git add cli/internal/cmd/auth_token.go cli/internal/cmd/auth_token_test.go cli/internal/cmd/login.go
git commit -m "feat(cli): add `hail auth token`"
```

---

## Task 10: Extend `hail tail` — positional id, email type, terminal email statuses, rename `singleCall` → `singleResource`

**Files:**

- Modify: `cli/internal/cmd/tail.go`
- Modify: `cli/internal/cmd/tail_test.go`

- [ ] **Step 1: Verify the email status enum on the server**

Run:

```bash
grep -nE "EmailStatus\s*=|EmailStatus.*Literal" /Users/r/playground/hail/core/hailhq/core/schemas.py
```

Note the terminal-shaped values (e.g. `sent`, `failed`, `delivered`, `bounced`). Reflect them into `tail.go` in step 3 — verify each name matches the enum exactly.

- [ ] **Step 2: Write failing tests**

Add to `cli/internal/cmd/tail_test.go`:

```go
func TestTail_PositionalCall_Equiv_FlagID(t *testing.T) {
	// Both forms should hit the same /events endpoint with the same id query.
	srv := newFakeServer(t, http.StatusOK, map[string]any{"items": []any{}, "next_cursor": nil})
	id := "11111111-1111-1111-1111-111111111111"

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"tail", "call:"+id, "--no-follow",
	)
	if err != nil {
		t.Fatalf("positional: %v", err)
	}
	q := srv.lastReq.URL.Query().Get("id")
	if q != "call:"+id {
		t.Fatalf("expected id query, got %q", q)
	}
}

func TestTail_PositionalEmail(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, map[string]any{"items": []any{}, "next_cursor": nil})
	id := "22222222-2222-2222-2222-222222222222"
	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"tail", "email:"+id, "--no-follow",
	)
	if err != nil {
		t.Fatalf("email positional: %v", err)
	}
	q := srv.lastReq.URL.Query().Get("id")
	if q != "email:"+id {
		t.Fatalf("expected email id query, got %q", q)
	}
}

func TestTail_PositionalAndFlagDisagree(t *testing.T) {
	a := "11111111-1111-1111-1111-111111111111"
	b := "22222222-2222-2222-2222-222222222222"
	_, stderr, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test"},
		"tail", "call:"+a, "--id", "call:"+b, "--no-follow",
	)
	if !errors.Is(err, errInvalidInputs) {
		t.Fatalf("want errInvalidInputs, got %v", err)
	}
	if !strings.Contains(stderr, "--id and positional disagree") {
		t.Fatalf("missing reason: %q", stderr)
	}
}

func TestTail_UnsupportedType(t *testing.T) {
	id := "11111111-1111-1111-1111-111111111111"
	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test"},
		"tail", "sms:"+id, "--no-follow",
	)
	if !errors.Is(err, errInvalidInputs) && !strings.Contains(err.Error(), "unsupported resource type") {
		t.Fatalf("want unsupported-type rejection, got %v", err)
	}
}
```

- [ ] **Step 3: Run to confirm failure**

Run: `cd cli && go test ./internal/cmd -run "TestTail_Positional|TestTail_UnsupportedType" -v`
Expected: FAIL — positional accepted only via `--id` currently; `email` not in supported list.

- [ ] **Step 4: Implement**

In `cli/internal/cmd/tail.go`:

1. Extend `supportedResourceTypes`:

```go
var supportedResourceTypes = []string{"call", "email"}
```

2. Add `terminalEmailStatuses` next to `terminalCallStatuses`. The exact enum values come from step 1 (`sent`, `failed`, `delivered`, `bounced` — verify against `core/schemas.py`):

```go
var terminalEmailStatuses = map[client.EventStreamResponseEmailStatus]bool{
	client.EventStreamResponseEmailStatusSent:      true,
	client.EventStreamResponseEmailStatusFailed:    true,
	client.EventStreamResponseEmailStatusDelivered: true,
	client.EventStreamResponseEmailStatusBounced:   true,
}
```

If the generated client doesn't yet expose `EmailStatus` on `EventStreamResponse`, regenerate the client first:

```bash
cd cli && go generate ./...
```

3. Change `Args: cobra.NoArgs` to accept up to one positional:

```go
Args: cobra.MaximumNArgs(1),
```

4. Inside `runTail`, accept positional and reconcile with `--id`. At the top of the function (after the interval check), insert:

```go
// Positional <type>:<uuid> is accepted alongside --id. They must agree.
var positional string
if len(args) == 1 {
	positional = args[0]
}
if positional != "" && f.id != "" && positional != f.id {
	return helpAndFail(cmd, "--id and positional disagree")
}
if f.id == "" {
	f.id = positional
}
```

The `cmd` and `args` aren't currently in scope of `runTail` (it's a private helper). Either:

- Pass `cmd, args` through (simplest), or
- Resolve the positional in the cobra `RunE` closure and set `f.id` before calling `runTail`.

Take option 2 to keep `runTail` clean. In `newTailCmd`:

```go
RunE: func(cmd *cobra.Command, args []string) error {
	if len(args) == 1 {
		if f.id != "" && args[0] != f.id {
			return helpAndFail(cmd, "--id and positional disagree")
		}
		f.id = args[0]
	}
	return runTail(cmd.Context(), opts, f)
},
```

5. Rename `singleCall` to `singleResource` everywhere it appears in `tail.go`. Update `renderEvent`'s signature and call sites.

6. Add the email auto-exit dispatch in the auto-exit block currently keyed on `singleCall && terminalCallStatuses[...]`:

```go
if singleResource {
	switch resourceType {
	case "call":
		if firstPage.CallStatus != nil && terminalCallStatuses[*firstPage.CallStatus] {
			renderSystemLine(opts, time.Now().UTC(), fmt.Sprintf("call %s", string(*firstPage.CallStatus)), colorize)
			return nil
		}
	case "email":
		if firstPage.EmailStatus != nil && terminalEmailStatuses[*firstPage.EmailStatus] {
			renderSystemLine(opts, time.Now().UTC(), fmt.Sprintf("email %s", string(*firstPage.EmailStatus)), colorize)
			return nil
		}
	}
}
```

If `firstPage.EmailStatus` doesn't exist on the generated `EventStreamResponse`, the API needs to surface it. Verify:

```bash
grep -nE "EmailStatus|email_status" cli/internal/client/client.gen.go core/hailhq/core/schemas.py | head
```

If absent server-side, scope it out: do not add `terminalEmailStatuses` and let email tails run until SIGINT. Document the omission in `tail.go` and remove the email auto-exit branch.

7. Update the unsupported-type error path to go through `helpAndFail`:

```go
if !supported {
	return uuid.Nil, nil, helpAndFail(cmd, fmt.Sprintf(
		"unsupported resource type %q; supported: %s",
		resType, strings.Join(supportedResourceTypes, ", "),
	))
}
```

(This pushes `cmd` into `parseResourceID`'s signature; alternative: surface the error normally and rely on the generic exit-1 path. Pick the latter to avoid threading `cmd` everywhere — the existing `--id` error path also returns a bare error; consistency wins.)

Keep `parseResourceID` as-is and let the existing bare-error path stand. The test `TestTail_UnsupportedType` asserts the substring `"unsupported resource type"` regardless of error type, which works either way.

- [ ] **Step 5: Run tests**

Run: `cd cli && go test ./internal/cmd -run TestTail -v`
Expected: PASS (existing + four new cases).

- [ ] **Step 6: Commit**

```bash
git add cli/internal/cmd/tail.go cli/internal/cmd/tail_test.go
git commit -m "feat(cli): hail tail accepts positional <type>:<uuid>; supports email"
```

---

## Task 11: `hail call tail <id>` sugar

**Files:**

- Create: `cli/internal/cmd/call_tail.go`
- Create: `cli/internal/cmd/call_tail_test.go`
- Modify: `cli/internal/cmd/call.go` (register subcommand)

- [ ] **Step 1: Write the failing test**

```go
// cli/internal/cmd/call_tail_test.go
package cmd

import (
	"net/http"
	"strings"
	"testing"
)

func TestCallTail_FullUUID_DelegatesToTail(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, map[string]any{"items": []any{}, "next_cursor": nil})
	id := "11111111-1111-1111-1111-111111111111"

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"call", "tail", id, "--no-follow",
	)
	if err != nil {
		t.Fatalf("unexpected: %v", err)
	}
	q := srv.lastReq.URL.Query().Get("id")
	if !strings.HasPrefix(q, "call:") || !strings.HasSuffix(q, id) {
		t.Fatalf("expected call:<id>, got %q", q)
	}
}

func TestCallTail_MissingArg_ShowsHelp(t *testing.T) {
	_, stderr, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test"},
		"call", "tail",
	)
	if err == nil || !strings.Contains(stderr, "missing required: <call-id>") {
		t.Fatalf("expected help-on-missing, got err=%v stderr=%q", err, stderr)
	}
}
```

- [ ] **Step 2: Run to confirm failure**

Run: `cd cli && go test ./internal/cmd -run TestCallTail -v`
Expected: FAIL — subcommand undefined.

- [ ] **Step 3: Implement**

```go
// cli/internal/cmd/call_tail.go
package cmd

import (
	"context"
	"fmt"

	"github.com/spf13/cobra"
)

// newCallTailCmd is sugar for `hail tail call:<uuid>` that adds prefix
// resolution. Body is intentionally tiny — the polling loop lives in
// tail.go.
func newCallTailCmd(opts *Options) *cobra.Command {
	f := &tailFlags{}
	cmd := &cobra.Command{
		Use:   "tail <call-id>",
		Short: "Follow the event stream for one call (alias for `hail tail call:<id>`)",
		Long: `hail call tail — follow events for one call.

<call-id> may be a full UUID or a 4+ char hex prefix. See ` + "`hail tail --help`" + `
for stream flags (--from-start, --no-follow, --interval, --kind).`,
		Args: argsOrHelp(1, "<call-id>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runCallTail(cmd.Context(), opts, f, args[0])
		},
	}
	// Mirror the polling-shape flags from `hail tail`. Defaults match.
	cmd.Flags().IntVar(&f.intervalMS, "interval", 500, "Poll interval in ms (100..10000)")
	cmd.Flags().BoolVar(&f.fromStart, "from-start", false, "Fetch all historical events first")
	cmd.Flags().BoolVar(&f.noFollow, "no-follow", false, "Print one page and exit")
	cmd.Flags().StringVar(&f.kind, "kind", "", "Filter by event kind")
	return cmd
}

func runCallTail(ctx context.Context, opts *Options, f *tailFlags, input string) error {
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}
	id, _, err := resolveCallID(ctx, apiClient, input)
	if err != nil {
		return err
	}
	f.id = fmt.Sprintf("call:%s", id.String())
	return runTail(ctx, opts, f)
}
```

Register in `cli/internal/cmd/call.go`'s `newCallCmd`, after the existing AddCommands:

```go
cmd.AddCommand(newCallStatusCmd(opts))
cmd.AddCommand(newCallListCmd(opts))
cmd.AddCommand(newCallTailCmd(opts))
```

- [ ] **Step 4: Run tests**

Run: `cd cli && go test ./internal/cmd -run TestCallTail -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cli/internal/cmd/call_tail.go cli/internal/cmd/call_tail_test.go cli/internal/cmd/call.go
git commit -m "feat(cli): add `hail call tail <id>` sugar"
```

---

## Task 12: `hail email tail <id>` sugar

**Files:**

- Create: `cli/internal/cmd/email_tail.go`
- Create: `cli/internal/cmd/email_tail_test.go`
- Modify: `cli/internal/cmd/email.go` (register subcommand)

- [ ] **Step 1: Write the failing test**

```go
// cli/internal/cmd/email_tail_test.go
package cmd

import (
	"net/http"
	"strings"
	"testing"
)

func TestEmailTail_FullUUID_DelegatesToTail(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, map[string]any{"items": []any{}, "next_cursor": nil})
	id := "22222222-2222-2222-2222-222222222222"

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "tail", id, "--no-follow",
	)
	if err != nil {
		t.Fatalf("unexpected: %v", err)
	}
	q := srv.lastReq.URL.Query().Get("id")
	if !strings.HasPrefix(q, "email:") || !strings.HasSuffix(q, id) {
		t.Fatalf("expected email:<id>, got %q", q)
	}
}

func TestEmailTail_MissingArg_ShowsHelp(t *testing.T) {
	_, stderr, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test"},
		"email", "tail",
	)
	if err == nil || !strings.Contains(stderr, "missing required: <email-id>") {
		t.Fatalf("expected help-on-missing, got err=%v stderr=%q", err, stderr)
	}
}
```

- [ ] **Step 2: Run to confirm failure**

Run: `cd cli && go test ./internal/cmd -run TestEmailTail -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

```go
// cli/internal/cmd/email_tail.go
package cmd

import (
	"context"
	"fmt"

	"github.com/spf13/cobra"
)

func newEmailTailCmd(opts *Options) *cobra.Command {
	f := &tailFlags{}
	cmd := &cobra.Command{
		Use:   "tail <email-id>",
		Short: "Follow the event stream for one email (alias for `hail tail email:<id>`)",
		Long: `hail email tail — follow events for one email.

<email-id> may be a full UUID or a 4+ char hex prefix. See ` + "`hail tail --help`" + `
for stream flags (--from-start, --no-follow, --interval, --kind).`,
		Args: argsOrHelp(1, "<email-id>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runEmailTail(cmd.Context(), opts, f, args[0])
		},
	}
	cmd.Flags().IntVar(&f.intervalMS, "interval", 500, "Poll interval in ms (100..10000)")
	cmd.Flags().BoolVar(&f.fromStart, "from-start", false, "Fetch all historical events first")
	cmd.Flags().BoolVar(&f.noFollow, "no-follow", false, "Print one page and exit")
	cmd.Flags().StringVar(&f.kind, "kind", "", "Filter by event kind")
	return cmd
}

func runEmailTail(ctx context.Context, opts *Options, f *tailFlags, input string) error {
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}
	id, err := resolveEmailID(ctx, apiClient, input)
	if err != nil {
		return err
	}
	f.id = fmt.Sprintf("email:%s", id.String())
	return runTail(ctx, opts, f)
}
```

Register in `cli/internal/cmd/email.go`:

```go
cmd.AddCommand(newEmailSendCmd(opts))
cmd.AddCommand(newEmailListCmd(opts))
cmd.AddCommand(newEmailGetCmd(opts))
cmd.AddCommand(newEmailTailCmd(opts))
cmd.AddCommand(newEmailDomainCmd(opts))
```

- [ ] **Step 4: Run tests**

Run: `cd cli && go test ./internal/cmd -run TestEmailTail -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cli/internal/cmd/email_tail.go cli/internal/cmd/email_tail_test.go cli/internal/cmd/email.go
git commit -m "feat(cli): add `hail email tail <id>` sugar"
```

---

## Task 13: `hail email raw <id>`

**Files:**

- Create: `cli/internal/cmd/email_raw.go`
- Create: `cli/internal/cmd/email_raw_test.go`
- Modify: `cli/internal/cmd/email.go` (register subcommand)

- [ ] **Step 1: Write the failing test**

```go
// cli/internal/cmd/email_raw_test.go
package cmd

import (
	"bytes"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
)

func newRawServer(t *testing.T, body []byte) *fakeServer {
	t.Helper()
	fs := &fakeServer{}
	fs.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&fs.hits, 1)
		fs.lastReq = r.Clone(r.Context())
		// The list endpoint is hit by prefix resolution; serve any GET /emails
		// without an id with a one-item list. The /emails/{id}/raw path serves
		// raw bytes.
		if strings.HasSuffix(r.URL.Path, "/raw") {
			w.Header().Set("Content-Type", "message/rfc822")
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write(body)
			return
		}
		if r.URL.Path == "/emails" {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_, _ = io.WriteString(w, `{"items":[{"id":"33333333-3333-3333-3333-333333333333"}],"next_cursor":null}`)
			return
		}
		w.WriteHeader(http.StatusNotFound)
	}))
	t.Cleanup(fs.Close)
	return fs
}

func TestEmailRaw_StdoutDefault(t *testing.T) {
	rfc := []byte("From: alice@example.com\r\nSubject: hi\r\n\r\nbody\r\n")
	srv := newRawServer(t, rfc)
	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "raw", "33333333-3333-3333-3333-333333333333",
	)
	if err != nil {
		t.Fatalf("unexpected: %v", err)
	}
	if !bytes.Equal([]byte(stdout), rfc) {
		t.Fatalf("body mismatch: got %q want %q", stdout, rfc)
	}
}

func TestEmailRaw_OutputToFile(t *testing.T) {
	rfc := []byte("From: bob@example.com\r\n\r\nfile body\r\n")
	srv := newRawServer(t, rfc)
	dir := t.TempDir()
	target := filepath.Join(dir, "out.eml")

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "raw", "33333333-3333-3333-3333-333333333333", "--output", target,
	)
	if err != nil {
		t.Fatalf("unexpected: %v", err)
	}
	got, err := os.ReadFile(target)
	if err != nil {
		t.Fatalf("read output: %v", err)
	}
	if !bytes.Equal(got, rfc) {
		t.Fatalf("file mismatch: got %q want %q", got, rfc)
	}
}

func TestEmailRaw_JSONRejected(t *testing.T) {
	srv := newRawServer(t, []byte(""))
	_, stderr, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "raw", "33333333-3333-3333-3333-333333333333", "--json",
	)
	if !errors.Is(err, errInvalidInputs) {
		t.Fatalf("want errInvalidInputs, got %v", err)
	}
	if !strings.Contains(stderr, "--json is not supported on raw") {
		t.Fatalf("expected reason: %q", stderr)
	}
}
```

- [ ] **Step 2: Run to confirm failure**

Run: `cd cli && go test ./internal/cmd -run TestEmailRaw -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

```go
// cli/internal/cmd/email_raw.go
package cmd

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"os"

	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/hail-hq/hail/cli/internal/client"
)

func newEmailRawCmd(opts *Options) *cobra.Command {
	var output string
	cmd := &cobra.Command{
		Use:   "raw <email-id>",
		Short: "Write the raw RFC 5322 source of one email",
		Long: `hail email raw — emit the original RFC 5322 source of an email.

By default writes to stdout (binary stream — pipe to formail / mhonarc /
a file). Use --output path.eml to write to disk; --output - is the same
as omitting --output.

<email-id> may be a full UUID or a 4+ char hex prefix.

--json is not supported (the output is a binary stream, not JSON).`,
		Args: argsOrHelp(1, "<email-id>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			if opts.JSON {
				return helpAndFail(cmd, "--json is not supported on raw — output is a binary stream")
			}
			return runEmailRaw(cmd.Context(), opts, args[0], output)
		},
	}
	cmd.Flags().StringVar(&output, "output", "", "Write to a file (use '-' or omit for stdout)")
	return cmd
}

func runEmailRaw(ctx context.Context, opts *Options, input, output string) error {
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}
	id, err := resolveEmailID(ctx, apiClient, input)
	if err != nil {
		return err
	}
	resp, err := apiClient.GetEmailRawEmailsEmailIdRawGet(
		ctx, openapi_types.UUID(id), &client.GetEmailRawEmailsEmailIdRawGetParams{},
	)
	if err != nil {
		return fmt.Errorf("email raw API: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return apiError(resp.StatusCode, body)
	}

	dst, closeDst, err := openOutput(opts, output)
	if err != nil {
		return err
	}
	defer closeDst()
	if _, err := io.Copy(dst, resp.Body); err != nil {
		return fmt.Errorf("write raw: %w", err)
	}
	return nil
}

// openOutput resolves an --output flag value to a writer. Returns the
// opts.Stdout when the flag is "" or "-", or opens the named file (mode
// 0644, truncating) and returns a closer for it. The returned close func
// is always safe to call.
func openOutput(opts *Options, path string) (io.Writer, func(), error) {
	if path == "" || path == "-" {
		return opts.Stdout, func() {}, nil
	}
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o644)
	if err != nil {
		return nil, func() {}, fmt.Errorf("open %s: %w", path, err)
	}
	return f, func() { _ = f.Close() }, nil
}
```

Register in `cli/internal/cmd/email.go`:

```go
cmd.AddCommand(newEmailRawCmd(opts))
```

- [ ] **Step 4: Run tests**

Run: `cd cli && go test ./internal/cmd -run TestEmailRaw -v`
Expected: PASS (3 cases).

- [ ] **Step 5: Commit**

```bash
git add cli/internal/cmd/email_raw.go cli/internal/cmd/email_raw_test.go cli/internal/cmd/email.go
git commit -m "feat(cli): add `hail email raw <id>` for RFC 5322 dumps"
```

---

## Task 14: `hail email attachment <id> <attachment-id>`

**Files:**

- Create: `cli/internal/cmd/email_attachment.go`
- Create: `cli/internal/cmd/email_attachment_test.go`
- Modify: `cli/internal/cmd/email.go` (register subcommand)

- [ ] **Step 1: Write the failing test**

```go
// cli/internal/cmd/email_attachment_test.go
package cmd

import (
	"bytes"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
)

func newAttachServer(t *testing.T, attID, payload string) *fakeServer {
	t.Helper()
	fs := &fakeServer{}
	fs.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&fs.hits, 1)
		fs.lastReq = r.Clone(r.Context())

		switch {
		case strings.Contains(r.URL.Path, "/attachments/"):
			w.Header().Set("Content-Type", "application/pdf")
			w.WriteHeader(http.StatusOK)
			_, _ = io.WriteString(w, payload)
		case strings.HasSuffix(r.URL.Path, "/emails/44444444-4444-4444-4444-444444444444"):
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_, _ = io.WriteString(w, `{
				"id":"44444444-4444-4444-4444-444444444444",
				"organization_id":"55555555-5555-5555-5555-555555555555",
				"from_address":"a@b","to_addresses":["c@d"],
				"subject":"x","status":"received","direction":"inbound",
				"requested_at":"2026-06-28T00:00:00Z",
				"attachments":[{"id":"`+attID+`","filename":"f.pdf","content_type":"application/pdf","size_bytes":4}]
			}`)
		case r.URL.Path == "/emails":
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_, _ = io.WriteString(w, `{"items":[{"id":"44444444-4444-4444-4444-444444444444"}],"next_cursor":null}`)
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	t.Cleanup(fs.Close)
	return fs
}

func TestEmailAttachment_OutputToFile(t *testing.T) {
	attID := "66666666-6666-6666-6666-666666666666"
	srv := newAttachServer(t, attID, "PDF1")
	dir := t.TempDir()
	target := filepath.Join(dir, "out.bin")

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "attachment",
		"44444444-4444-4444-4444-444444444444",
		attID,
		"--output", target,
	)
	if err != nil {
		t.Fatalf("unexpected: %v", err)
	}
	got, err := os.ReadFile(target)
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	if !bytes.Equal(got, []byte("PDF1")) {
		t.Fatalf("body mismatch: %q", got)
	}
}

func TestEmailAttachment_PrefixMatchedWithinParent(t *testing.T) {
	attID := "77777777-7777-7777-7777-777777777777"
	srv := newAttachServer(t, attID, "PAYLOAD")
	dir := t.TempDir()
	target := filepath.Join(dir, "out.bin")

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "attachment",
		"44444444",   // prefix
		"7777",       // prefix
		"--output", target,
	)
	if err != nil {
		t.Fatalf("unexpected: %v", err)
	}
	got, _ := os.ReadFile(target)
	if !bytes.Equal(got, []byte("PAYLOAD")) {
		t.Fatalf("body mismatch: %q", got)
	}
}
```

Note: testing the TTY-refusal path requires mocking the isatty check; defer that to a follow-up unit test on the helper rather than gating Task 14 on it. The piped-stdout path (default in `runRoot` since stdout is a `bytes.Buffer`, not a TTY) is already covered by the absence of an `--output` flag in the simpler scenario — which would conflict with my TTY guard. Resolution: only enforce the guard when `opts.Stdout` is `*os.File` AND a TTY. The test harness passes a `*bytes.Buffer`, so the guard is skipped automatically.

- [ ] **Step 2: Run to confirm failure**

Run: `cd cli && go test ./internal/cmd -run TestEmailAttachment -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

```go
// cli/internal/cmd/email_attachment.go
package cmd

import (
	"context"
	"encoding/hex"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"

	"github.com/google/uuid"
	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/hail-hq/hail/cli/internal/client"
)

func newEmailAttachmentCmd(opts *Options) *cobra.Command {
	var output string
	cmd := &cobra.Command{
		Use:   "attachment <email-id> <attachment-id>",
		Short: "Download one attachment as raw bytes",
		Long: `hail email attachment — fetch a single attachment.

By default writes to stdout (binary stream). Use --output path to write
to a file. If stdout is a TTY and no --output is set, the command
refuses to write so you don't print binary garbage to your terminal.

<email-id> and <attachment-id> may each be a full UUID or a 4+ char
hex prefix. Attachment prefix is resolved within the parent email's
attachment list (no list endpoint exists for attachments alone).

--json is not supported (binary stream).`,
		Args: argsOrHelp(2, "<email-id> <attachment-id>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			if opts.JSON {
				return helpAndFail(cmd, "--json is not supported on attachment — output is a binary stream")
			}
			return runEmailAttachment(cmd.Context(), cmd, opts, args[0], args[1], output)
		},
	}
	cmd.Flags().StringVar(&output, "output", "", "Write to a file (use '-' or omit for stdout)")
	return cmd
}

func runEmailAttachment(ctx context.Context, cmd *cobra.Command, opts *Options, emailInput, attachInput, output string) error {
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}

	emailID, err := resolveEmailID(ctx, apiClient, emailInput)
	if err != nil {
		return err
	}

	// Resolve the attachment prefix within the parent email's attachment list.
	parent, err := apiClient.GetEmailEmailsEmailIdGetWithResponse(
		ctx, openapi_types.UUID(emailID), &client.GetEmailEmailsEmailIdGetParams{},
	)
	if err != nil {
		return fmt.Errorf("email API: %w", err)
	}
	if parent.HTTPResponse.StatusCode != http.StatusOK || parent.JSON200 == nil {
		return apiError(parent.HTTPResponse.StatusCode, parent.Body)
	}

	attachID, err := matchAttachmentPrefix(parent.JSON200.Attachments, attachInput)
	if err != nil {
		return err
	}

	// Refuse to write binary to a TTY — same guard `curl` uses.
	if output == "" || output == "-" {
		if f, ok := opts.Stdout.(*os.File); ok {
			if fi, err := f.Stat(); err == nil && (fi.Mode()&os.ModeCharDevice) != 0 {
				return helpAndFail(cmd, "stdout is a TTY; pass --output <path> or pipe to a file")
			}
		}
	}

	resp, err := apiClient.GetEmailAttachmentEmailsEmailIdAttachmentsAttachmentIdGet(
		ctx, openapi_types.UUID(emailID), openapi_types.UUID(attachID),
		&client.GetEmailAttachmentEmailsEmailIdAttachmentsAttachmentIdGetParams{},
	)
	if err != nil {
		return fmt.Errorf("attachment API: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return apiError(resp.StatusCode, body)
	}

	dst, closeDst, err := openOutput(opts, output)
	if err != nil {
		return err
	}
	defer closeDst()
	if _, err := io.Copy(dst, resp.Body); err != nil {
		return fmt.Errorf("write attachment: %w", err)
	}
	return nil
}

func matchAttachmentPrefix(atts *[]client.EmailAttachmentResponse, input string) (uuid.UUID, error) {
	if atts == nil || len(*atts) == 0 {
		return uuid.Nil, fmt.Errorf("email has no attachments")
	}
	if parsed, err := uuid.Parse(input); err == nil {
		for _, a := range *atts {
			if uuid.UUID(a.Id) == parsed {
				return parsed, nil
			}
		}
		return uuid.Nil, fmt.Errorf("attachment %s not found on this email", parsed)
	}
	needle := strings.ToLower(strings.ReplaceAll(input, "-", ""))
	if len(needle) < 4 || !isHex(needle) {
		return uuid.Nil, fmt.Errorf("invalid attachment id %q (expected full UUID or 4+ char hex prefix)", input)
	}
	var matches []uuid.UUID
	for _, a := range *atts {
		id := uuid.UUID(a.Id)
		if strings.HasPrefix(hex.EncodeToString(id[:]), needle) {
			matches = append(matches, id)
		}
	}
	switch len(matches) {
	case 0:
		return uuid.Nil, fmt.Errorf("no attachment matches prefix %q", input)
	case 1:
		return matches[0], nil
	default:
		names := make([]string, 0, len(matches))
		for _, m := range matches {
			names = append(names, m.String())
		}
		return uuid.Nil, fmt.Errorf("ambiguous prefix %q matches %d attachments: %s",
			input, len(matches), strings.Join(names, ", "))
	}
}
```

Register in `cli/internal/cmd/email.go`:

```go
cmd.AddCommand(newEmailAttachmentCmd(opts))
```

- [ ] **Step 4: Run tests**

Run: `cd cli && go test ./internal/cmd -run TestEmailAttachment -v`
Expected: PASS (2 cases).

- [ ] **Step 5: Commit**

```bash
git add cli/internal/cmd/email_attachment.go cli/internal/cmd/email_attachment_test.go cli/internal/cmd/email.go
git commit -m "feat(cli): add `hail email attachment` for inbound binary fetches"
```

---

## Task 15: Remove `hail webhooks` outright

**Files:**

- Modify: `cli/internal/cmd/webhooks.go`
- Modify: `cli/internal/cmd/webhooks_test.go`

- [ ] **Step 1: Write the failing test**

Append to `cli/internal/cmd/webhooks_test.go`:

```go
func TestWebhooks_List_PrintsDeprecationBanner(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, map[string]any{"items": []any{}, "next_cursor": nil})
	_, stderr, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"webhooks", "list",
	)
	if err != nil {
		t.Fatalf("unexpected: %v", err)
	}
	if !strings.Contains(stderr, "hail webhooks` is deprecated") {
		t.Fatalf("expected deprecation banner: %q", stderr)
	}
}

func TestWebhooks_RootHelpDoesNotShowWebhooks(t *testing.T) {
	stdout, _, err := runRoot(t, map[string]string{"HAIL_API_KEY": "sk_test"}, "--help")
	if err != nil {
		t.Fatalf("unexpected: %v", err)
	}
	// The custom root help in Task 4 doesn't list every subcommand. To
	// verify Hidden:true is in effect, fall back to cobra's default usage
	// for the root subtree by reading from the parent command.
	// Simpler: assert `hail --help` does not include "webhooks" in any
	// rendering path. The dynamic Long shows only Common: anyway.
	if strings.Contains(stdout, "webhooks") {
		t.Fatalf("did not expect 'webhooks' in root help: %q", stdout)
	}
}
```

- [ ] **Step 2: Run to confirm failure**

Run: `cd cli && go test ./internal/cmd -run TestWebhooks_(List_PrintsDeprecation|RootHelpDoesNotShow) -v`
Expected: FAIL — no banner today.

- [ ] **Step 3: Implement**

Edit `cli/internal/cmd/webhooks.go`. At the top of `newWebhooksCmd`, add `Hidden: true` and define a banner string + helper that subcommands call:

```go
const webhooksDeprecationBanner = "hail: `hail webhooks` is deprecated and will be removed in 0.7.0.\n" +
	"      Manage webhook subscriptions from the console."

func emitWebhooksDeprecation(opts *Options) {
	fmt.Fprintln(opts.Stderr, webhooksDeprecationBanner)
}

func newWebhooksCmd(opts *Options) *cobra.Command {
	cmd := &cobra.Command{
		Use:    "webhooks",
		Hidden: true,
		Short:  "Manage outbound webhook subscriptions (DEPRECATED — use the console)",
		Long: `hail webhooks — DEPRECATED.

This subtree is being removed in 0.7.0. Manage webhook subscriptions from
the console. Self-hosters debug delivery state via SQL on the webhook_*
tables.

Subcommands remain available in 0.6.x with a deprecation banner.`,
	}
	cmd.AddCommand(newWebhooksCreateCmd(opts))
	cmd.AddCommand(newWebhooksListCmd(opts))
	cmd.AddCommand(newWebhooksDeliveriesCmd(opts))
	cmd.AddCommand(newWebhooksRedeliverCmd(opts))
	return cmd
}
```

Inside each subcommand `RunE`, call `emitWebhooksDeprecation(opts)` as the first line before any other work. Example for `newWebhooksCreateCmd`:

```go
RunE: func(cmd *cobra.Command, _ []string) error {
	emitWebhooksDeprecation(opts)
	return runWebhooksCreate(cmd.Context(), opts, f)
},
```

Apply to all four leaf subcommands.

- [ ] **Step 4: Run tests**

Run: `cd cli && go test ./internal/cmd -run TestWebhooks -v`
Expected: PASS for all (existing + two new).

- [ ] **Step 5: Commit**

```bash
git add cli/internal/cmd/webhooks.go cli/internal/cmd/webhooks_test.go
git commit -m "refactor(cli): deprecate `hail webhooks` (hidden + banner)"
```

---

## Task 16: Subcommand `--help` doesn't change; sanity full-suite

**Files:**

- (no changes — verification step)

- [ ] **Step 1: Run the full test suite**

```bash
cd cli && go test ./... -count=1
```

Expected: PASS for all packages.

- [ ] **Step 2: Build the binary and exercise help manually**

```bash
cd cli && go build -o /tmp/hail . && \
  /tmp/hail --help && echo --- && \
  /tmp/hail call --help && echo --- && \
  /tmp/hail email --help && echo --- && \
  /tmp/hail tail --help && echo --- && \
  /tmp/hail auth --help
```

Visually confirm:

- Root help shows "Get started: hail login" (no creds in the env you're running from).
- `webhooks` does not appear under `hail --help`'s rendered output but `hail webhooks --help` still works.
- Each subcommand's help is intact.

- [ ] **Step 3: No-op commit (or skip if nothing changed)**

If the build surfaced fixups needed (unused imports, typos), stage and commit them under a single message:

```bash
git add -p cli/
git commit -m "chore(cli): post-merge cleanup from DX pass"
```

Otherwise, skip the commit.

---

## Task 17: Docs — README and operations.md

**Files:**

- Modify: `README.md`
- Modify: `docs/operations.md`

- [ ] **Step 1: Inspect current CLI references**

```bash
grep -nE "hail (login|call|email|tail|webhooks|version|completion|mcp|auth)" README.md docs/operations.md
```

- [ ] **Step 2: Update `README.md`**

Replace the CLI snippet block with one that reflects the new tree. Keep it on one screen (repo tenet). Suggested shape:

````markdown
## CLI

```bash
hail login                        # authenticate (device flow)
hail auth logout                  # remove local credentials
hail auth token                   # print bare API key for scripting

hail call +14155550100 --prompt "be brief"
hail call list
hail call tail <id>               # follow events for one call

hail email send --to a@b.com --subject hi --body "hello"
hail email list
hail email get <id>
hail email tail <id>              # follow events for one email
hail email raw <id>               # RFC 5322 source
hail email attachment <id> <att-id> --output file.pdf
hail email domain register --kind hail_mail
hail email domain list

hail tail                         # cross-channel event stream
hail tail call:<id>               # narrow by resource type

hail mcp endpoint                 # Streamable HTTP URL for the MCP server
hail completion zsh               # source <(hail completion zsh)
hail version
```
````

````

- [ ] **Step 3: Update `docs/operations.md`**

The self-host auth section currently describes `HAIL_API_KEY` bootstrap. Add one paragraph after the bootstrap section:

```markdown
### `hail auth` subcommands

For interactive sessions on a managed Hail deployment:

- `hail login` — device-authorization flow, persists `~/.hail/credentials.json`.
- `hail auth logout` — delete the local credentials file (idempotent).
- `hail auth token` — print the bare API key; use in scripts as
  `export HAIL_API_KEY=$(hail auth token)`.

Self-hosters typically skip the device flow and set `HAIL_API_KEY`
directly per the [Self-hosted auth](#self-hosted-auth) section above.
````

- [ ] **Step 4: Commit**

```bash
git add README.md docs/operations.md
git commit -m "docs(cli): reflect DX pass — new subcommands and auth flow"
```

---

## Self-review checklist (done after all tasks)

- [ ] Spec coverage: every spec section maps to at least one task (tenets → Tasks 1-4, auth → 8-9, tail+inbound → 10-14, discoverability → 5-7, rollout → 15, docs → 17).
- [ ] No placeholders: search for "TBD", "TODO", "..." in this plan.
- [ ] Type consistency: `helpAndFail`/`requireInputs`/`argsOrHelp`/`errInvalidInputs`/`errNotAuthenticated`/`requireAuth` are spelled identically across every task that uses them.
- [ ] All generated client method names verified against `cli/internal/client/client.gen.go` (`GetEmailRawEmailsEmailIdRawGet`, `GetEmailAttachmentEmailsEmailIdAttachmentsAttachmentIdGet`, `GetEmailEmailsEmailIdGetWithResponse`).
- [ ] `EmailStatus`-shaped names on the generated `EventStreamResponse` verified before Task 10 lands; if absent, Task 10's email auto-exit is removed per the conditional in step 4.
