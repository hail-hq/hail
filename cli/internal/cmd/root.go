// Package cmd implements the `hail` CLI subcommand tree.
package cmd

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"

	"github.com/spf13/cobra"

	"github.com/hail-hq/hail/cli/internal/client"
)

// utcTSLayout is the wall-clock format used in human-readable CLI output.
const utcTSLayout = "2006-01-02 15:04:05Z"

// Version metadata. These vars are overwritten at link time by GoReleaser
// (see cli/.goreleaser.yml -> ldflags) when building a tagged release. Local
// `go build` keeps the defaults below, so `hail --version` prints
// "dev (commit none, built unknown)" until released.
var (
	version   = "dev"
	commit    = "none"
	buildDate = "unknown"
)

// DefaultAPIURL is used when neither --api-url nor HAIL_API_URL is set.
const DefaultAPIURL = "http://localhost:8080"

// Options bundles the resolved environment + flags for subcommands. Subcommands
// receive these via cobra's Command.RunE closure rather than reading globals.
// This keeps tests free of process-state leaks.
type Options struct {
	APIURL string
	APIKey string
	JSON   bool
	Stdout io.Writer
	Stderr io.Writer
}

// NewRootCmd builds the root cobra.Command. All IO is injected: tests provide
// their own stdout/stderr buffers and an environment lookup function.
//
// getenv may be nil, in which case os.Getenv is used. This indirection is what
// lets the MissingAPIKey test exercise the empty-env path deterministically.
func NewRootCmd(stdout, stderr io.Writer, getenv func(string) string) *cobra.Command {
	if getenv == nil {
		getenv = os.Getenv
	}
	if stdout == nil {
		stdout = os.Stdout
	}
	if stderr == nil {
		stderr = os.Stderr
	}

	opts := &Options{Stdout: stdout, Stderr: stderr}

	root := &cobra.Command{
		Use:           "hail",
		Short:         "hail — universal communication platform for AI agents",
		SilenceUsage:  true,
		SilenceErrors: true,
		PersistentPreRunE: func(cmd *cobra.Command, args []string) error {
			if opts.APIURL == "" {
				opts.APIURL = getenv("HAIL_API_URL")
			}
			if opts.APIURL == "" {
				opts.APIURL = DefaultAPIURL
			}
			if opts.APIKey == "" {
				opts.APIKey = getenv("HAIL_API_KEY")
			}
			return nil
		},
	}
	root.Version = fmt.Sprintf("%s (commit %s, built %s)", version, commit, buildDate)
	root.SetOut(stdout)
	root.SetErr(stderr)

	root.PersistentFlags().StringVar(&opts.APIURL, "api-url", "", "API base URL (default: $HAIL_API_URL or "+DefaultAPIURL+")")
	root.PersistentFlags().StringVar(&opts.APIKey, "api-key", "", "API key (default: $HAIL_API_KEY)")
	root.PersistentFlags().BoolVar(&opts.JSON, "json", false, "Output JSON instead of human-friendly text")

	root.AddCommand(newCallCmd(opts))
	root.AddCommand(newTailCmd(opts))

	return root
}

// newClient builds an OpenAPI client with the auth header already wired and
// any extra request editors appended. Subcommands call it instead of
// re-doing the empty-key check + ClientWithResponses dance.
func (o *Options) newClient(extra ...client.RequestEditorFn) (*client.ClientWithResponses, error) {
	if o.APIKey == "" {
		return nil, errors.New("missing API key: set HAIL_API_KEY or pass --api-key")
	}
	editors := append([]client.RequestEditorFn{authEditor(o.APIKey)}, extra...)
	clientOpts := make([]client.ClientOption, len(editors))
	for i, e := range editors {
		clientOpts[i] = client.WithRequestEditorFn(e)
	}
	c, err := client.NewClientWithResponses(o.APIURL, clientOpts...)
	if err != nil {
		return nil, fmt.Errorf("client init: %w", err)
	}
	return c, nil
}

// printJSON emits an indented JSON encoding of v on the writer.
func printJSON(w io.Writer, v any) error {
	out, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return fmt.Errorf("encode JSON: %w", err)
	}
	fmt.Fprintln(w, string(out))
	return nil
}

// strPtr returns nil for an empty string and a pointer to s otherwise. Used
// by subcommands when building optional string fields on request bodies.
func strPtr(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

// Execute parses os.Args and runs the root command. It is the binary entry
// point and the only place that calls os.Exit, so subcommand handlers can
// remain pure (return error, propagate up).
//
// SIGINT (Ctrl-C) during a long-running subcommand surfaces as
// errInterrupted from that subcommand; we exit 130 (POSIX convention for
// "killed by SIGINT") and skip the error message — no half-formed line.
func Execute() {
	root := NewRootCmd(os.Stdout, os.Stderr, os.Getenv)
	if err := root.Execute(); err != nil {
		if errors.Is(err, errInterrupted) {
			os.Exit(130)
		}
		fmt.Fprintln(os.Stderr, "hail:", err)
		os.Exit(1)
	}
}
