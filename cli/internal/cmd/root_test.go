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
