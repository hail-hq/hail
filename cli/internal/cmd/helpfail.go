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
	// Route Help() onto stderr — usage chatter triggered by invalid inputs
	// belongs on stderr so stdout stays reserved for actual program output.
	// Save / restore the writer rather than mutating the command for the
	// rest of its life.
	origOut := cmd.OutOrStdout()
	cmd.SetOut(cmd.ErrOrStderr())
	_ = cmd.Help()
	cmd.SetOut(origOut)
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

// requireMarkedFlags runs cmd.ValidateRequiredFlags() (the check driven by
// MarkFlagRequired) as a PreRunE, so a missing flag gets the same
// reason+Help()+errInvalidInputs treatment as every other input-validation
// failure in this CLI. Set as PreRunE, this runs before Cobra's own
// automatic ValidateRequiredFlags call later in Command.execute(), so ours
// wins — Cobra's generic "required flag(s) ... not set" (no help printed,
// exit 1, not 2) never surfaces to the user.
func requireMarkedFlags(cmd *cobra.Command, _ []string) error {
	if err := cmd.ValidateRequiredFlags(); err != nil {
		return helpAndFail(cmd, err.Error())
	}
	return nil
}

// requireTrueFlag closes the gap MarkFlagRequired leaves on bool flags:
// ValidateRequiredFlags (driven by MarkFlagRequired, via requireMarkedFlags)
// only checks pflag.Changed, so `--recipient-consent=false` satisfies it
// even though the API requires the value to actually be true. Call this
// from RunE — after requireMarkedFlags has already run in PreRunE — for
// any bool flag that must not just be present but true. Mirrors the
// existing --to shape: MarkFlagRequired catches omission, this (like
// normalizeRecipients' empty check) catches "passed but not usable", and
// both failure paths converge on the same reason+Help()+errInvalidInputs
// treatment via helpAndFail.
func requireTrueFlag(cmd *cobra.Command, value bool, name string) error {
	if value {
		return nil
	}
	return helpAndFail(cmd, name+" must be true — the API rejects this request otherwise")
}
