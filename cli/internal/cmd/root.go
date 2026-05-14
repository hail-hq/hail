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

// DefaultAPIURL is the global API target when neither --api-url, $HAIL_API_URL,
// nor a stored credentials file pin a value. The CLI talks to production by
// default; self-hosters opt out explicitly via --api-url / $HAIL_API_URL.
//
// The fallback is applied lazily in newClient (and in `hail login`'s
// credential-save step) rather than in PersistentPreRunE, so subcommands can
// distinguish "user/env/creds pinned an API URL" (opts.APIURL non-empty) from
// "nothing was supplied" (opts.APIURL == ""). Without that distinction,
// `hail login` cannot tell whether to honor an explicit --api-url override.
const DefaultAPIURL = "https://api.hail.so"

// Options bundles the resolved environment + flags for subcommands. Subcommands
// receive these via cobra's Command.RunE closure rather than reading globals.
// This keeps tests free of process-state leaks.
type Options struct {
	APIURL string
	APIKey string
	JSON   bool
	Stdout io.Writer
	Stderr io.Writer
	// Getenv is the env-lookup func injected by NewRootCmd. Subcommands that
	// need to read env vars must go through this (not os.Getenv) so tests can
	// drive them deterministically via the runRoot helper.
	Getenv func(string) string
}

// ResolvedAPIURL returns o.APIURL when set, otherwise DefaultAPIURL. Centralizes
// the lazy-default policy referenced by both newClient and `hail login` — see
// the DefaultAPIURL doc-comment for why the fallback is deferred to call sites.
func (o *Options) ResolvedAPIURL() string {
	if o.APIURL != "" {
		return o.APIURL
	}
	return DefaultAPIURL
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

	opts := &Options{Stdout: stdout, Stderr: stderr, Getenv: getenv}

	root := &cobra.Command{
		Use:   "hail",
		Short: "hail — universal communication platform for AI agents",
		Long: `hail — universal communication platform for AI agents.

Works with Hail Cloud (the managed offering at https://hail.so) and self-hosted
deployments. Cloud users authenticate via ` + "`hail login`" + `; self-hosters seed an
API key into their local stack (see docs/operations.md) and set HAIL_API_KEY
or pass --api-key.`,
		SilenceUsage:  true,
		SilenceErrors: true,
		PersistentPreRunE: func(cmd *cobra.Command, args []string) error {
			// Resolution order:
			//   --api-key > $HAIL_API_KEY > ~/.hail/credentials.json
			//   --api-url > $HAIL_API_URL > ~/.hail/credentials.json
			//
			// DefaultAPIURL is NOT applied here — newClient (and login's
			// credential-save step) fall back to it when opts.APIURL is empty.
			// Keeping "no signal" distinguishable from "user pinned a URL"
			// is what lets `hail login` honor an explicit --api-url.
			if opts.APIURL == "" {
				opts.APIURL = getenv("HAIL_API_URL")
			}
			if opts.APIKey == "" {
				opts.APIKey = getenv("HAIL_API_KEY")
			}
			if opts.APIKey == "" || opts.APIURL == "" {
				creds, err := loadCredentials()
				if err != nil {
					return err
				}
				if creds != nil && opts.APIKey == "" {
					opts.APIKey = creds.APIKey
				}
				if creds != nil && opts.APIURL == "" {
					opts.APIURL = creds.APIURL
				}
			}
			return nil
		},
	}
	root.Version = fmt.Sprintf("%s (commit %s, built %s)", version, commit, buildDate)
	root.SetOut(stdout)
	root.SetErr(stderr)

	root.PersistentFlags().StringVar(&opts.APIURL, "api-url", "", "API base URL (default: $HAIL_API_URL or ~/.hail/credentials.json or "+DefaultAPIURL+")")
	root.PersistentFlags().StringVar(&opts.APIKey, "api-key", "", "API key (default: $HAIL_API_KEY or ~/.hail/credentials.json — see 'hail login')")
	root.PersistentFlags().BoolVar(&opts.JSON, "json", false, "Output JSON instead of human-friendly text")

	root.AddCommand(newCallCmd(opts))
	root.AddCommand(newTailCmd(opts))
	root.AddCommand(newLoginCmd(opts))
	root.AddCommand(newAuthCmd(opts))

	return root
}

// newClient builds an OpenAPI client with the auth header already wired and
// any extra request editors appended. Subcommands call it instead of
// re-doing the empty-key check + ClientWithResponses dance.
func (o *Options) newClient(extra ...client.RequestEditorFn) (*client.ClientWithResponses, error) {
	if o.APIKey == "" {
		return nil, errors.New("missing API key: run `hail login`, set HAIL_API_KEY, or pass --api-key")
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
