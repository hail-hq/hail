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
