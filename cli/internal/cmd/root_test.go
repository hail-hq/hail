package cmd

import (
	"strings"
	"testing"
)

func TestRootHelp_NotAuthenticated_LeadsWithLoginHint(t *testing.T) {
	// Sandbox HOME so loadCredentials() can't pick up a real ~/.hail/credentials.json
	// from the dev machine and flip this case into the authenticated branch.
	t.Setenv("HOME", t.TempDir())

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
	// Symmetry with the unauthenticated test: keep ~/.hail out of the picture
	// so the assertions depend only on the env map we pass in.
	t.Setenv("HOME", t.TempDir())

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

// Regression guard: the custom HelpFunc on root must NOT drop cobra's
// auto-rendered "Available Commands:" listing. An earlier implementation
// replaced the whole help body with a hand-written banner, so `hail --help`
// silently hid mcp/auth/version/completion. We now compose the banner +
// cmd.UsageString() so every top-level subcommand appears.
func TestRootHelp_ListsAllTopLevelSubcommands(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": "https://api.hail.so"},
		"--help",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(stdout, "Available Commands:") {
		t.Fatalf("expected cobra's Available Commands block: %q", stdout)
	}
	for _, want := range []string{"auth", "call", "completion", "email", "mcp", "tail", "version"} {
		if !strings.Contains(stdout, want) {
			t.Errorf("expected subcommand %q in `hail --help`: %q", want, stdout)
		}
	}
}

// TestHelp_CallSplitsRequiredOneOfAndOptionalFlags pins the
// splitRequiredFlags/markOneOfRequired template wiring for `hail call`:
// --recipient-consent (MarkFlagRequired) renders under "Required flags:",
// the mode A/B group (markOneOfRequired) renders under "Required (one
// of):", and everything else stays under "Optional flags:".
func TestHelp_CallSplitsRequiredOneOfAndOptionalFlags(t *testing.T) {
	stdout, _, err := runRoot(t, map[string]string{}, "call", "--help")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	reqIdx := strings.Index(stdout, "Required flags:")
	oneOfIdx := strings.Index(stdout, "Required (one of):")
	optIdx := strings.Index(stdout, "Optional flags:")
	if reqIdx == -1 || oneOfIdx == -1 || optIdx == -1 {
		t.Fatalf("expected all three sections present: %q", stdout)
	}
	if !(reqIdx < oneOfIdx && oneOfIdx < optIdx) {
		t.Fatalf("expected section order Required, Required (one of), Optional; got: %q", stdout)
	}

	required := stdout[reqIdx:oneOfIdx]
	if !strings.Contains(required, "--recipient-consent") {
		t.Errorf("expected --recipient-consent under Required flags: %q", required)
	}

	oneOf := stdout[oneOfIdx:optIdx]
	for _, want := range []string{"--prompt", "--llm-url", "--llm-key", "--llm-model"} {
		if !strings.Contains(oneOf, want) {
			t.Errorf("expected %q under Required (one of): %q", want, oneOf)
		}
	}

	optional := stdout[optIdx:]
	for _, notWant := range []string{"--recipient-consent", "--prompt", "--llm-url", "--llm-key", "--llm-model"} {
		if strings.Contains(optional, notWant) {
			t.Errorf("did not expect %q under Optional flags: %q", notWant, optional)
		}
	}
	for _, want := range []string{"--from", "--first-message", "--message-type"} {
		if !strings.Contains(optional, want) {
			t.Errorf("expected %q under Optional flags: %q", want, optional)
		}
	}
}

// TestHelp_EmailSendSplitsRequiredOneOfAndOptionalFlags mirrors the call
// test above for `hail email send`'s body group.
func TestHelp_EmailSendSplitsRequiredOneOfAndOptionalFlags(t *testing.T) {
	stdout, _, err := runRoot(t, map[string]string{}, "email", "send", "--help")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	reqIdx := strings.Index(stdout, "Required flags:")
	oneOfIdx := strings.Index(stdout, "Required (one of):")
	optIdx := strings.Index(stdout, "Optional flags:")
	if reqIdx == -1 || oneOfIdx == -1 || optIdx == -1 {
		t.Fatalf("expected all three sections present: %q", stdout)
	}
	if !(reqIdx < oneOfIdx && oneOfIdx < optIdx) {
		t.Fatalf("expected section order Required, Required (one of), Optional; got: %q", stdout)
	}

	required := stdout[reqIdx:oneOfIdx]
	for _, want := range []string{"--recipient-consent", "--subject", "--to"} {
		if !strings.Contains(required, want) {
			t.Errorf("expected %q under Required flags: %q", want, required)
		}
	}

	oneOf := stdout[oneOfIdx:optIdx]
	for _, want := range []string{"--body ", "--body-html", "--body-file", "--body-html-file"} {
		if !strings.Contains(oneOf, want) {
			t.Errorf("expected %q under Required (one of): %q", want, oneOf)
		}
	}

	optional := stdout[optIdx:]
	for _, notWant := range []string{"--recipient-consent", "--subject", "--to "} {
		if strings.Contains(optional, notWant) {
			t.Errorf("did not expect %q under Optional flags: %q", notWant, optional)
		}
	}
	for _, want := range []string{"--cc", "--bcc", "--from", "--reply-to"} {
		if !strings.Contains(optional, want) {
			t.Errorf("expected %q under Optional flags: %q", want, optional)
		}
	}
}

// TestHelp_NeitherRequiredNorOneOf_FallsBackToPlainFlagsSection pins the
// unchanged-behavior guarantee: a command using neither MarkFlagRequired
// nor markOneOfRequired (e.g. `call status`) still renders the single
// alphabetical "Flags:" block, not the three-way split.
func TestHelp_NeitherRequiredNorOneOf_FallsBackToPlainFlagsSection(t *testing.T) {
	stdout, _, err := runRoot(t, map[string]string{}, "call", "status", "--help")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(stdout, "\nFlags:\n") {
		t.Fatalf("expected plain Flags: section: %q", stdout)
	}
	if strings.Contains(stdout, "Required flags:") || strings.Contains(stdout, "Required (one of):") {
		t.Fatalf("did not expect a required split for a command with no required/one-of flags: %q", stdout)
	}
}
