package cmd

import (
	"context"
	"fmt"
	"net/http"

	"github.com/google/uuid"
	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/hail-hq/hail/cli/internal/client"
)

func newCallStatusCmd(opts *Options) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "status <call-id>",
		Short: "Fetch the current status of a call",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runCallStatus(cmd.Context(), opts, args[0])
		},
	}
	return cmd
}

func runCallStatus(ctx context.Context, opts *Options, idStr string) error {
	parsed, err := uuid.Parse(idStr)
	if err != nil {
		return fmt.Errorf("invalid call id %q: %w", idStr, err)
	}
	callID := openapi_types.UUID(parsed)

	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}

	resp, err := apiClient.GetCallCallsCallIdGetWithResponse(
		ctx, callID, &client.GetCallCallsCallIdGetParams{},
	)
	if err != nil {
		return fmt.Errorf("call API: %w", err)
	}
	if resp.HTTPResponse.StatusCode == http.StatusNotFound {
		return fmt.Errorf("call %s not found (or not in your org)", idStr)
	}
	if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
		return apiErrorGeneric(resp.HTTPResponse.StatusCode, resp.Body)
	}

	return printCallStatus(opts, resp.JSON200)
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

// apiErrorGeneric formats a non-success response without depending on the
// specific endpoint's typed JSON422 field.
func apiErrorGeneric(status int, body []byte) error {
	if len(body) > 0 && len(body) < 1024 {
		return fmt.Errorf("API error %d: %s", status, string(body))
	}
	return fmt.Errorf("API error %d", status)
}
