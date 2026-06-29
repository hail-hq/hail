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
