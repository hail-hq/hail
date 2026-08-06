package cmd

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"text/tabwriter"

	"github.com/spf13/cobra"

	"github.com/hail-hq/hail/cli/internal/client"
)

// newProvidersCmd builds the `providers` subtree — standing (per-org) BYO
// provider config for the three pipeline layers: llm, tts, stt.
//
// The organization is always the one behind the API key; it appears in no
// path and no body, so a key can only ever reach its own rows.
//
// Keys are write-only end to end: the API stores them encrypted and returns
// only the last four characters plus a set-at timestamp, so no `providers`
// subcommand can print a key back. `--key -` reads the key from stdin,
// which keeps it out of shell history and out of the process table.
func newProvidersCmd(opts *Options) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "providers",
		Short: "Manage the org's standing BYO provider config (llm/tts/stt)",
		Long: `hail providers — bring your own LLM, TTS, or STT provider.

Saved config applies to every call the org places, unless that call carries
a per-call 'llm' block (see 'hail call --llm-url'). Precedence is per-call,
then standing, then Hail's own models.

Layers:
  llm   the brain. Providers: openai-compatible (needs --base-url),
        anthropic, google.
  tts   speech synthesis. Providers: cartesia, elevenlabs.
  stt   transcription. Providers: deepgram, speechmatics.

Keys are write-only — reads show only the last four characters. Pass
'--key -' to read the key from stdin instead of the command line.

Example:
  printf '%s' "$OPENAI_KEY" | hail providers set llm \
    --provider openai-compatible \
    --base-url https://api.openai.com/v1 \
    --model gpt-4.1-mini \
    --key -`,
	}
	cmd.AddCommand(newProvidersListCmd(opts))
	cmd.AddCommand(newProvidersSetCmd(opts))
	cmd.AddCommand(newProvidersDeleteCmd(opts))
	cmd.AddCommand(newProvidersActivateCmd(opts))
	cmd.AddCommand(newProvidersTestCmd(opts))
	return cmd
}

// --------------------------------------------------------------------------- //
// list
// --------------------------------------------------------------------------- //

func newProvidersListCmd(opts *Options) *cobra.Command {
	return &cobra.Command{
		Use:     "list",
		Aliases: []string{"ls"},
		Short:   "List every saved provider config, all layers",
		Args:    cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runProvidersList(cmd.Context(), opts)
		},
	}
}

func runProvidersList(ctx context.Context, opts *Options) error {
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}

	resp, err := apiClient.ListProvidersWithResponse(ctx, &client.ListProvidersParams{})
	if err != nil {
		return fmt.Errorf("providers API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}
	return printProviderList(opts, resp.JSON200)
}

// --------------------------------------------------------------------------- //
// set
// --------------------------------------------------------------------------- //

type providersSetFlags struct {
	provider string
	model    string
	baseURL  string
	key      string
	fallback bool
}

func newProvidersSetCmd(opts *Options) *cobra.Command {
	f := &providersSetFlags{}
	cmd := &cobra.Command{
		Use:   "set <layer>",
		Short: "Save a provider for a layer and make it the active one",
		Long: `hail providers set — save (or update) a provider and activate it.

<layer> is llm, tts, or stt.

Omit --key to edit the model/base-url without resending the key: the stored
key survives. Pass '--key -' to read the key from stdin so it never lands in
shell history:

  printf '%s' "$MY_KEY" | hail providers set llm --provider anthropic \
    --model claude-sonnet-4-5 --key -

--fallback lets a failure of your provider fall through to Hail's own keys
(off by default — silently billing Hail's models would defeat the point of
bringing your own).`,
		Args:    argsOrHelp(1, "<layer>"),
		PreRunE: requireMarkedFlags,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runProvidersSet(cmd.Context(), cmd, opts, f, args[0])
		},
	}
	cmd.Flags().StringVar(&f.provider, "provider", "", "Provider name for the layer (e.g. openai-compatible, cartesia, deepgram)")
	cmd.Flags().StringVar(&f.model, "model", "", "Model identifier to run on that provider")
	cmd.Flags().StringVar(&f.baseURL, "base-url", "", "HTTPS endpoint — required by the openai-compatible LLM provider")
	cmd.Flags().StringVar(&f.key, "key", "", "Provider API key; '-' reads it from stdin. Omit to keep the stored key")
	cmd.Flags().BoolVar(&f.fallback, "fallback", false, "Fall back to Hail's own keys when this provider fails")
	cmd.MarkFlagRequired("provider")
	cmd.MarkFlagRequired("model")
	return cmd
}

func runProvidersSet(ctx context.Context, cmd *cobra.Command, opts *Options, f *providersSetFlags, layer string) error {
	apiKey, err := resolveProviderKey(f.key, cmd.InOrStdin())
	if err != nil {
		return err
	}

	params := map[string]interface{}{"model": f.model}
	if f.baseURL != "" {
		params["base_url"] = f.baseURL
	}
	body := client.ProviderConfigUpsert{
		Provider:        f.provider,
		Params:          &params,
		FallbackEnabled: &f.fallback,
		ApiKey:          strPtr(apiKey),
	}

	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}

	resp, err := apiClient.UpsertProviderWithResponse(ctx, layer, &client.UpsertProviderParams{}, body)
	if err != nil {
		return fmt.Errorf("providers API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}
	return printProviderEntry(opts, resp.JSON200, "saved")
}

// resolveProviderKey turns the --key flag into the value to send. "-" means
// "read the whole of stdin", so the key stays out of shell history and out
// of the process table; the trailing newline a shell here-string or `echo`
// adds is trimmed, since no provider key ends in one. An empty flag means
// "don't send a key at all" — the server keeps whatever is stored.
func resolveProviderKey(flag string, stdin io.Reader) (string, error) {
	if flag != "-" {
		return flag, nil
	}
	raw, err := readAll(stdin)
	if err != nil {
		return "", fmt.Errorf("read key from stdin: %w", err)
	}
	key := strings.TrimRight(raw, "\r\n")
	if key == "" {
		return "", fmt.Errorf("--key - given but stdin was empty")
	}
	return key, nil
}

// --------------------------------------------------------------------------- //
// delete
// --------------------------------------------------------------------------- //

func newProvidersDeleteCmd(opts *Options) *cobra.Command {
	return &cobra.Command{
		Use:     "delete <layer> <provider>",
		Aliases: []string{"rm"},
		Short:   "Delete one saved provider config",
		Long: `hail providers delete — remove one provider's saved config.

Deleting the active provider promotes the most recently updated sibling in
that layer; deleting the last one leaves the layer on Hail's own models.`,
		Args: argsOrHelp(2, "<layer> <provider>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runProvidersDelete(cmd.Context(), opts, args[0], args[1])
		},
	}
}

func runProvidersDelete(ctx context.Context, opts *Options, layer, provider string) error {
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}

	resp, err := apiClient.DeleteProviderWithResponse(ctx, layer, provider, &client.DeleteProviderParams{})
	if err != nil {
		return fmt.Errorf("providers API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusNoContent {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}
	fmt.Fprintf(opts.Stdout, "✓ Provider %s deleted from layer %s\n", provider, layer)
	return nil
}

// --------------------------------------------------------------------------- //
// activate
// --------------------------------------------------------------------------- //

type providersActivateFlags struct {
	provider string
}

func newProvidersActivateCmd(opts *Options) *cobra.Command {
	f := &providersActivateFlags{}
	cmd := &cobra.Command{
		Use:   "activate <layer>",
		Short: "Switch which saved provider a layer uses",
		Long: `hail providers activate — make an already-saved provider the active one.

The provider must already have a saved config in that layer (save one with
'hail providers set'); otherwise the API returns 404.`,
		Args:    argsOrHelp(1, "<layer>"),
		PreRunE: requireMarkedFlags,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runProvidersActivate(cmd.Context(), opts, f, args[0])
		},
	}
	cmd.Flags().StringVar(&f.provider, "provider", "", "Saved provider to activate")
	cmd.MarkFlagRequired("provider")
	return cmd
}

func runProvidersActivate(ctx context.Context, opts *Options, f *providersActivateFlags, layer string) error {
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}

	body := client.ProviderActivateRequest{Provider: f.provider}
	resp, err := apiClient.ActivateProviderWithResponse(ctx, layer, &client.ActivateProviderParams{}, body)
	if err != nil {
		return fmt.Errorf("providers API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}
	return printProviderEntry(opts, resp.JSON200, "activated")
}

// --------------------------------------------------------------------------- //
// test
// --------------------------------------------------------------------------- //

type providersTestFlags struct {
	provider string
}

func newProvidersTestCmd(opts *Options) *cobra.Command {
	f := &providersTestFlags{}
	cmd := &cobra.Command{
		Use:   "test <layer>",
		Short: "Probe a saved provider key against the real provider",
		Long: `hail providers test — check a stored key actually works.

With no --provider, tests the layer's active provider. The key never leaves
the server: Hail decrypts the stored key and calls the provider itself.`,
		Args: argsOrHelp(1, "<layer>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runProvidersTest(cmd.Context(), opts, f, args[0])
		},
	}
	cmd.Flags().StringVar(&f.provider, "provider", "", "Saved provider to test (default: the layer's active one)")
	return cmd
}

func runProvidersTest(ctx context.Context, opts *Options, f *providersTestFlags, layer string) error {
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}

	body := client.ProviderValidateRequest{Provider: strPtr(f.provider)}
	resp, err := apiClient.ValidateProviderWithResponse(ctx, layer, &client.ValidateProviderParams{}, body)
	if err != nil {
		return fmt.Errorf("providers API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}

	if opts.JSON {
		return printJSON(opts.Stdout, resp.JSON200)
	}
	mark := "✓"
	if resp.JSON200.Status != "valid" {
		mark = "✗"
	}
	fmt.Fprintf(opts.Stdout, "%s %s: %s\n", mark, layer, resp.JSON200.Status)
	if resp.JSON200.Message != nil && *resp.JSON200.Message != "" {
		fmt.Fprintf(opts.Stdout, "  %s\n", *resp.JSON200.Message)
	}
	return nil
}

// --------------------------------------------------------------------------- //
// printers
// --------------------------------------------------------------------------- //

func printProviderEntry(opts *Options, p *client.ProviderConfigEntry, verb string) error {
	if opts.JSON {
		return printJSON(opts.Stdout, p)
	}

	fmt.Fprintf(opts.Stdout, "✓ Provider %s: %s (%s)\n", verb, p.Provider, string(p.Layer))
	fmt.Fprintf(opts.Stdout, "  Active:   %t\n", p.IsActive)
	fmt.Fprintf(opts.Stdout, "  Fallback: %t\n", p.FallbackEnabled)
	if params := formatProviderParams(p.Params); params != "" {
		fmt.Fprintf(opts.Stdout, "  Params:   %s\n", params)
	}
	fmt.Fprintf(opts.Stdout, "  Key:      %s\n", formatProviderKey(p.KeyLast4, p.KeySetAt))
	return nil
}

func printProviderList(opts *Options, body *client.ProviderConfigListResponse) error {
	if opts.JSON {
		return printJSON(opts.Stdout, body)
	}
	if len(body.Providers) == 0 {
		fmt.Fprintln(opts.Stdout, "(no saved providers — calls use Hail's own models)")
		return nil
	}

	w := tabwriter.NewWriter(opts.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(w, "LAYER\tPROVIDER\tACTIVE\tFALLBACK\tKEY\tPARAMS")
	for _, p := range body.Providers {
		active := ""
		if p.IsActive {
			active = "*"
		}
		fmt.Fprintf(w, "%s\t%s\t%s\t%t\t%s\t%s\n",
			string(p.Layer), p.Provider, active, p.FallbackEnabled,
			formatProviderKey(p.KeyLast4, p.KeySetAt), formatProviderParams(p.Params))
	}
	if err := w.Flush(); err != nil {
		return fmt.Errorf("write table: %w", err)
	}
	return nil
}

// formatProviderKey renders the only key-derived facts the API ever hands
// back: the last four characters and when the key was set.
func formatProviderKey(last4, setAt *string) string {
	if last4 == nil || *last4 == "" {
		return "(none)"
	}
	out := "…" + *last4
	if setAt != nil && *setAt != "" {
		out += " (set " + *setAt + ")"
	}
	return out
}

// formatProviderParams renders the layer-shaped params blob compactly. It is
// free-form on the wire (its schema depends on the layer), so print the JSON
// rather than pretending to know the keys.
func formatProviderParams(params map[string]interface{}) string {
	if len(params) == 0 {
		return ""
	}
	out, err := json.Marshal(params)
	if err != nil {
		return ""
	}
	return string(out)
}
