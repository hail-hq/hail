package cmd

import (
	"context"
	"encoding/json"
	"fmt"
	"text/tabwriter"
	"time"

	"github.com/spf13/cobra"

	"github.com/hail-hq/hail/cli/internal/client"
)

// newEmailEventsCmd builds `hail email events <email-id>` — the delivery
// and engagement timeline for one email (sent, delivered, bounced, opened,
// clicked, ...). Complements `hail email tail`, which follows the same
// stream live; `events` is the one-shot, already-happened view.
func newEmailEventsCmd(opts *Options) *cobra.Command {
	return &cobra.Command{
		Use:   "events <email-id>",
		Short: "Show the delivery/engagement timeline for one email",
		Long: `hail email events — show the delivery/engagement timeline for one email.

<email-id> may be a full UUID or a 4+ char hex prefix (same resolution rules
as ` + "`hail email get`" + `).`,
		Args: argsOrHelp(1, "<email-id>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runEmailEvents(cmd.Context(), opts, args[0])
		},
	}
}

func runEmailEvents(ctx context.Context, opts *Options, input string) error {
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}
	id, err := resolveEmailID(ctx, apiClient, input)
	if err != nil {
		return err
	}

	resp, err := apiClient.ListEmailEventsEmailsEmailIdEventsGetWithResponse(
		ctx, id, &client.ListEmailEventsEmailsEmailIdEventsGetParams{},
	)
	if err != nil {
		return fmt.Errorf("email API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != 200 || resp.JSON200 == nil {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}

	return printEmailEvents(opts, resp.JSON200)
}

func printEmailEvents(opts *Options, body *client.EmailEventListResponse) error {
	if opts.JSON {
		return printJSON(opts.Stdout, body)
	}
	if len(body.Items) == 0 {
		fmt.Fprintln(opts.Stdout, "(no events)")
		return nil
	}

	w := tabwriter.NewWriter(opts.Stdout, 0, 4, 2, ' ', 0)
	fmt.Fprintln(w, "KIND\tOCCURRED AT\tDETAIL")
	for _, e := range body.Items {
		detail := "{}"
		if len(e.Payload) > 0 {
			if raw, err := json.Marshal(e.Payload); err == nil {
				detail = string(raw)
			}
		}
		fmt.Fprintf(w, "%s\t%s\t%s\n", string(e.Kind), e.OccurredAt.UTC().Format(time.RFC3339), detail)
	}
	return w.Flush()
}
