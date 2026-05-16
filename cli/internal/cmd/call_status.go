package cmd

import (
	"context"
	"fmt"
	"net/http"
	"strings"

	"github.com/google/uuid"
	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/hail-hq/hail/cli/internal/client"
)

func newCallStatusCmd(opts *Options) *cobra.Command {
	cmd := &cobra.Command{
		Use:     "status <call-id>",
		Aliases: []string{"get"},
		Short:   "Fetch the current status of a call",
		Long: `hail call status — show one call.

<call-id> may be either a full UUID or a hex prefix (e.g. the 8-char id
shown by 'hail call list'). Prefix lookups scan the 200 most recent calls
and fail if the prefix is ambiguous or matches nothing — pass the full
UUID to look up older calls unambiguously.`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runCallStatus(cmd.Context(), opts, args[0])
		},
	}
	return cmd
}

func runCallStatus(ctx context.Context, opts *Options, idStr string) error {
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}

	parsed, err := resolveCallID(ctx, apiClient, idStr)
	if err != nil {
		return err
	}
	callID := openapi_types.UUID(parsed)

	resp, err := apiClient.GetCallCallsCallIdGetWithResponse(
		ctx, callID, &client.GetCallCallsCallIdGetParams{},
	)
	if err != nil {
		return fmt.Errorf("call API: %w", err)
	}
	if resp.HTTPResponse.StatusCode == http.StatusNotFound {
		return fmt.Errorf("call %s not found (or not in your org)", parsed.String())
	}
	if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}

	return printCallStatus(opts, resp.JSON200)
}

// resolveCallID returns a full UUID for input. If input parses as a UUID it
// is returned directly; otherwise it is treated as a hex prefix and matched
// against the 200 most recent calls. Dashes in the prefix are stripped before
// matching, so both 11111111 and 11111111-1111 resolve the same UUID.
//
// Mirrors `git rev-parse`'s short-hash behavior: ambiguous prefixes and
// no-match cases surface as CLI errors with the candidate list / hint.
func resolveCallID(ctx context.Context, apiClient *client.ClientWithResponses, input string) (uuid.UUID, error) {
	if parsed, err := uuid.Parse(input); err == nil {
		return parsed, nil
	}
	needle := strings.ToLower(strings.ReplaceAll(input, "-", ""))
	if len(needle) < 4 || len(needle) > 31 || !isHex(needle) {
		return uuid.Nil, fmt.Errorf("invalid call id %q (expected full UUID or hex prefix of 4+ chars)", input)
	}

	limit := 200
	resp, err := apiClient.ListCallsCallsGetWithResponse(ctx, &client.ListCallsCallsGetParams{Limit: &limit})
	if err != nil {
		return uuid.Nil, fmt.Errorf("resolve call prefix: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
		return uuid.Nil, apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}

	var matches []uuid.UUID
	for _, c := range resp.JSON200.Items {
		if strings.HasPrefix(strings.ReplaceAll(c.Id.String(), "-", ""), needle) {
			matches = append(matches, uuid.UUID(c.Id))
		}
	}

	switch len(matches) {
	case 1:
		return matches[0], nil
	case 0:
		return uuid.Nil, fmt.Errorf("no call matches prefix %q (searched %d recent calls); pass the full UUID for older calls", input, len(resp.JSON200.Items))
	default:
		names := make([]string, 0, len(matches))
		for _, m := range matches {
			names = append(names, m.String())
		}
		return uuid.Nil, fmt.Errorf("ambiguous prefix %q matches %d calls: %s", input, len(matches), strings.Join(names, ", "))
	}
}

func isHex(s string) bool {
	for _, c := range s {
		switch {
		case c >= '0' && c <= '9':
		case c >= 'a' && c <= 'f':
		default:
			return false
		}
	}
	return true
}

// printCallStatus renders a CallResponse for the `status` subcommand. Format
// mirrors the post-placement output (see printCall) but adds the post-call
// fields ended/end_reason/recording when present.
func printCallStatus(opts *Options, call *client.CallResponse) error {
	if opts.JSON {
		return printJSON(opts.Stdout, call)
	}

	fmt.Fprintf(opts.Stdout, "Call:    %s\n", call.Id.String())
	fmt.Fprintf(opts.Stdout, "  From:    %s\n", call.FromE164)
	fmt.Fprintf(opts.Stdout, "  To:      %s\n", call.ToE164)
	fmt.Fprintf(opts.Stdout, "  Status:  %s\n", string(call.Status))
	fmt.Fprintf(opts.Stdout, "  Requested: %s\n", call.RequestedAt.UTC().Format(utcTSLayout))
	if call.StartedAt != nil {
		fmt.Fprintf(opts.Stdout, "  Started:   %s\n", call.StartedAt.UTC().Format(utcTSLayout))
	}
	if call.AnsweredAt != nil {
		fmt.Fprintf(opts.Stdout, "  Answered:  %s\n", call.AnsweredAt.UTC().Format(utcTSLayout))
	}
	if call.EndedAt != nil {
		fmt.Fprintf(opts.Stdout, "  Ended:     %s\n", call.EndedAt.UTC().Format(utcTSLayout))
	}
	if call.EndReason != nil && *call.EndReason != "" {
		fmt.Fprintf(opts.Stdout, "  End reason: %s\n", *call.EndReason)
	}
	if call.RecordingS3Key != nil && *call.RecordingS3Key != "" {
		fmt.Fprintf(opts.Stdout, "  Recording: %s\n", *call.RecordingS3Key)
	}
	return nil
}
