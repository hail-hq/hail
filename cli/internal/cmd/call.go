package cmd

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/hail-hq/hail/cli/internal/client"
)

// callFlags are the values bound by `hail call` — kept as a struct so the test
// suite can poke individual fields without leaking cobra wiring.
type callFlags struct {
	prompt         string
	llmURL         string
	llmKey         string
	llmModel       string
	from           string
	firstMessage   string
	idempotencyKey string
}

// newCallCmd builds the `call` command tree.
//
// Cobra layout: option 1 — the parent itself takes a phone-number positional
// argument (`hail call +1...`) and ALSO hosts sibling subcommands `status`
// and `list`. Cobra dispatches to a matching subcommand when args[0] equals
// its name; otherwise the parent's RunE handles the placement. Phone numbers
// begin with `+`, never colliding with `status`/`list`, so the disambiguation
// is unambiguous in practice and the existing `hail call <num>` invocation in
// the README + test suite keeps working unchanged.
//
// Tailing graduated to a top-level `hail tail` because the universal-comms
// roadmap (SMS, email) has events flowing through one stream, not per call.
func newCallCmd(opts *Options) *cobra.Command {
	f := &callFlags{}

	cmd := &cobra.Command{
		Use:   "call <to-number>",
		Short: "Place an outbound phone call (or use a subcommand)",
		Long: `hail call — place an outbound phone call

Provide either:
  --prompt <text>            (mode A — Hail's bundled fallback LLM)

or all three of:
  --llm-url, --llm-key, --llm-model   (mode B — bring your own OpenAI-compatible endpoint)

Mode A and mode B are mutually exclusive; supply exactly one.

Subcommands:
  hail call status <id>      Fetch the current status of a call.
  hail call list             List recent calls (cursor-paginated).

To stream events, see ` + "`hail tail`" + ` (top-level).`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runCall(cmd.Context(), opts, f, args[0])
		},
	}

	cmd.Flags().StringVar(&f.prompt, "prompt", "", "System prompt for the agent (mode A)")
	cmd.Flags().StringVar(&f.llmURL, "llm-url", "", "OpenAI-compatible base URL (mode B)")
	cmd.Flags().StringVar(&f.llmKey, "llm-key", "", "API key for the endpoint (mode B)")
	cmd.Flags().StringVar(&f.llmModel, "llm-model", "", "Model name (mode B)")
	cmd.Flags().StringVar(&f.from, "from", "", "Override the from-number (default: first active number on the org)")
	cmd.Flags().StringVar(&f.firstMessage, "first-message", "", "Spoken on pickup before listening")
	cmd.Flags().StringVar(&f.idempotencyKey, "idempotency-key", "", "Defaults to a fresh UUID")

	cmd.AddCommand(newCallStatusCmd(opts))
	cmd.AddCommand(newCallListCmd(opts))

	return cmd
}

func runCall(ctx context.Context, opts *Options, f *callFlags, toNumber string) error {
	if err := validateMode(f); err != nil {
		return err
	}

	body := client.CallCreate{
		To:           toNumber,
		SystemPrompt: strPtr(f.prompt),
		From:         strPtr(f.from),
		FirstMessage: strPtr(f.firstMessage),
	}
	if f.llmURL != "" {
		body.Llm = &client.LLMConfig{
			BaseUrl: f.llmURL,
			ApiKey:  f.llmKey,
			Model:   f.llmModel,
		}
	}

	idem := f.idempotencyKey
	if idem == "" {
		idem = uuid.NewString()
	}

	apiClient, err := opts.newClient(idempotencyEditor(idem))
	if err != nil {
		return err
	}

	resp, err := apiClient.CreateCallCallsPostWithResponse(ctx, &client.CreateCallCallsPostParams{}, body)
	if err != nil {
		return fmt.Errorf("call API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusCreated || resp.JSON201 == nil {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}

	return printCall(opts, resp.JSON201)
}

// validateMode enforces that exactly one of mode A or mode B is in play.
func validateMode(f *callFlags) error {
	hasPrompt := f.prompt != ""
	hasAnyLLM := f.llmURL != "" || f.llmKey != "" || f.llmModel != ""
	hasFullLLM := f.llmURL != "" && f.llmKey != "" && f.llmModel != ""

	if hasPrompt && hasAnyLLM {
		return errors.New("--prompt and --llm-* are mutually exclusive (use one mode)")
	}
	if !hasPrompt && !hasAnyLLM {
		return errors.New("must provide either --prompt or all of --llm-url --llm-key --llm-model")
	}
	if hasAnyLLM && !hasFullLLM {
		return errors.New("--llm-url, --llm-key, and --llm-model must all be supplied together")
	}
	return nil
}

func authEditor(key string) client.RequestEditorFn {
	return func(_ context.Context, req *http.Request) error {
		req.Header.Set("Authorization", "Bearer "+key)
		return nil
	}
}

func idempotencyEditor(key string) client.RequestEditorFn {
	return func(_ context.Context, req *http.Request) error {
		// Only attach to mutating requests; oapi-codegen has no metadata for
		// http method intent so we gate on POST/PUT/PATCH/DELETE.
		switch req.Method {
		case http.MethodPost, http.MethodPut, http.MethodPatch, http.MethodDelete:
			req.Header.Set("Idempotency-Key", key)
		}
		return nil
	}
}

// apiError translates a non-success response into a CLI-facing error. On 422
// the body is parsed as HTTPValidationError and detail messages are joined;
// otherwise we include the body when small or fall back to the status code.
func apiError(status int, body []byte) error {
	if status == http.StatusUnprocessableEntity {
		var v client.HTTPValidationError
		if err := json.Unmarshal(body, &v); err == nil && v.Detail != nil && len(*v.Detail) > 0 {
			msgs := make([]string, 0, len(*v.Detail))
			for _, d := range *v.Detail {
				msgs = append(msgs, d.Msg)
			}
			return fmt.Errorf("API error %d: %s", status, strings.Join(msgs, "; "))
		}
	}
	if len(body) > 0 && len(body) < 1024 {
		return fmt.Errorf("API error %d: %s", status, string(body))
	}
	return fmt.Errorf("API error %d", status)
}

// printCall renders the success response in either JSON or human form.
//
// JSON output marshals the parsed CallResponse rather than echoing the raw
// server bytes, so output is stable across server-side whitespace changes.
func printCall(opts *Options, call *client.CallResponse) error {
	if opts.JSON {
		return printJSON(opts.Stdout, call)
	}

	fmt.Fprintf(opts.Stdout, "✓ Call queued: %s\n", call.Id.String())
	fmt.Fprintf(opts.Stdout, "  From:    %s\n", call.FromE164)
	fmt.Fprintf(opts.Stdout, "  To:      %s\n", call.ToE164)
	fmt.Fprintf(opts.Stdout, "  Status:  %s\n", string(call.Status))
	fmt.Fprintf(opts.Stdout, "  Track:   hail call status %s\n", call.Id.String())
	return nil
}
