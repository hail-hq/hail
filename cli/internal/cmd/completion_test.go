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
		t.Fatalf("expected bash completion preamble: %q", truncateForLog(stdout, 200))
	}
}

func TestCompletion_Zsh(t *testing.T) {
	stdout, _, err := runRoot(t, map[string]string{}, "completion", "zsh")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(stdout, "compdef") {
		t.Fatalf("expected zsh compdef directive: %q", truncateForLog(stdout, 200))
	}
}

func TestCompletion_Fish(t *testing.T) {
	stdout, _, err := runRoot(t, map[string]string{}, "completion", "fish")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(stdout, "complete -c hail") {
		t.Fatalf("expected fish completion: %q", truncateForLog(stdout, 200))
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

func truncateForLog(s string, n int) string {
	if len(s) < n {
		return s
	}
	return s[:n]
}
