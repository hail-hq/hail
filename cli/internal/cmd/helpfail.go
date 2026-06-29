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
