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

// errNotAuthenticated is returned by requireAuth when the resolved
// Options carry no API key from any source (--api-key, $HAIL_API_KEY,
// or credentials file). Execute() recognizes it and prints the canonical
// "run hail login" hint before exiting 2.
var errNotAuthenticated = errors.New("not authenticated")

// requireAuth gates auth-requiring subcommands. Call it at the top of
// RunE before any I/O; commands that tolerate no auth (login, version,
// completion, mcp endpoint) skip it entirely.
func requireAuth(opts *Options) error {
	if opts.APIKey != "" {
		return nil
	}
	return errNotAuthenticated
}

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

	// Dynamic Long: the banner branches on whether creds resolved to anything.
	// Cobra does NOT run PersistentPreRunE before printing help, so we resolve
	// the auth state inline here. Keeps the help render free of side effects
	// beyond reading creds.
	//
	// Capture cobra's default help func before overriding so subcommand --help
	// can delegate without recursing into this closure (cmd.Root().HelpFunc()
	// would otherwise return the closure itself).
	defaultHelp := root.HelpFunc()
	root.SetHelpFunc(func(cmd *cobra.Command, args []string) {
		// Only customize the root help; subcommand --help paths stay default.
		if cmd != root {
			defaultHelp(cmd, args)
			return
		}
		apiKey := opts.APIKey
		apiURL := opts.APIURL
		if apiKey == "" {
			apiKey = getenv("HAIL_API_KEY")
		}
		if apiURL == "" {
			apiURL = getenv("HAIL_API_URL")
		}
		if apiKey == "" || apiURL == "" {
			if creds, _ := loadCredentials(); creds != nil {
				if apiKey == "" {
					apiKey = creds.APIKey
				}
				if apiURL == "" {
					apiURL = creds.APIURL
				}
			}
		}
		if apiURL == "" {
			apiURL = DefaultAPIURL
		}

		out := cmd.OutOrStdout()
		fmt.Fprintln(out, "hail — universal communication platform for AI agents.")
		fmt.Fprintln(out)
		if apiKey == "" {
			fmt.Fprintln(out, "Get started:")
			fmt.Fprintln(out, "  hail login          Authenticate with Hail")
		} else {
			fmt.Fprintf(out, "Signed in as %s → %s\n", maskAPIKey(apiKey), apiURL)
		}
		fmt.Fprintln(out)
		// Standard cobra usage block follows: Usage line, the full
		// Available Commands listing (auth, call, completion, email,
		// login, mcp, tail, version, ...), global flags, and the
		// "Use `hail [command] --help`" footer. Anything we hand-roll
		// here would drift from the actual subcommand tree.
		fmt.Fprint(out, cmd.UsageString())
	})

	root.PersistentFlags().StringVar(&opts.APIURL, "api-url", "", "API base URL (default: $HAIL_API_URL or ~/.hail/credentials.json or "+DefaultAPIURL+")")
	root.PersistentFlags().StringVar(&opts.APIKey, "api-key", "", "API key (default: $HAIL_API_KEY or ~/.hail/credentials.json — see 'hail login')")
	root.PersistentFlags().BoolVar(&opts.JSON, "json", false, "Output JSON instead of human-friendly text")

	root.AddCommand(newCallCmd(opts))
	root.AddCommand(newEmailCmd(opts))
	root.AddCommand(newTailCmd(opts))
	root.AddCommand(newLoginCmd(opts))
	root.AddCommand(newAuthCmd(opts))
	root.AddCommand(newMcpCmd(opts))
	root.AddCommand(newVersionCmd(opts))
	root.AddCommand(newCompletionCmd(opts))

	return root
}

// newClient builds an OpenAPI client with the auth header already wired and
// any extra request editors appended. Subcommands call it instead of
// re-doing the empty-key check + ClientWithResponses dance.
func (o *Options) newClient(extra ...client.RequestEditorFn) (*client.ClientWithResponses, error) {
	if err := requireAuth(o); err != nil {
		return nil, err
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
// Exit-code policy:
//
//	0  — success
//	2  — input validation failed (errInvalidInputs) or unauthenticated
//	     (errNotAuthenticated). Distinct from a generic failure so scripts
//	     can branch on "user error" vs "everything else".
//	130 — SIGINT (errInterrupted), POSIX convention.
//	1  — generic error.
func Execute() {
	root := NewRootCmd(os.Stdout, os.Stderr, os.Getenv)
	if err := root.Execute(); err != nil {
		switch {
		case errors.Is(err, errInterrupted):
			os.Exit(130)
		case errors.Is(err, errInvalidInputs):
			// Reason + Help() were already printed by helpAndFail. Exit silently.
			os.Exit(2)
		case errors.Is(err, errNotAuthenticated):
			fmt.Fprintln(os.Stderr, "hail: not authenticated.")
			fmt.Fprintln(os.Stderr)
			fmt.Fprintln(os.Stderr, "  Run `hail login` to authenticate, or set HAIL_API_KEY / pass --api-key.")
			os.Exit(2)
		default:
			fmt.Fprintln(os.Stderr, "hail:", err)
			os.Exit(1)
		}
	}
}

// maskAPIKey returns the prefix-•…-last4 form used in help banners.
// Conservative: if the input is too short to mask safely, returns "(set)"
// so we never leak a partial secret that would itself be useful.
func maskAPIKey(k string) string {
	if len(k) < 12 {
		return "(set)"
	}
	// hl_live_ prefix is 8 chars; preserve it + suffix.
	return k[:8] + "•…" + k[len(k)-4:]
}
