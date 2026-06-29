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
