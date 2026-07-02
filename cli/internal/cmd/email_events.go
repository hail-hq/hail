package cmd

import (
	"context"
	"encoding/json"
	"fmt"
	"text/tabwriter"
	"time"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/hail-hq/hail/cli/internal/client"
)

// newEmailEventsCmd builds `hail email events <email-id>` — the delivery
// and engagement timeline for one email (sent, delivered, bounced, opened,
// clicked, ...). Complements `hail email tail`, which follows the same
// stream live; `events` is the one-shot, already-happened view.
type emailEventsFlags struct {
	limit  int
	cursor string
	all    bool
}

func newEmailEventsCmd(opts *Options) *cobra.Command {
	f := &emailEventsFlags{}
	cmd := &cobra.Command{
		Use:   "events <email-id>",
		Short: "Show the delivery/engagement timeline for one email",
		Long: `hail email events — show the delivery/engagement timeline for one email.

<email-id> may be a full UUID or a 4+ char hex prefix (same resolution rules
as ` + "`hail email get`" + `).`,
		Args: argsOrHelp(1, "<email-id>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runEmailEvents(cmd.Context(), opts, f, args[0])
		},
	}
	cmd.Flags().IntVar(&f.limit, "limit", 100, "Page size (1..1000)")
	cmd.Flags().StringVar(&f.cursor, "cursor", "", "Resume from a previous next_cursor")
	cmd.Flags().BoolVar(&f.all, "all", false, "Walk every page")
	return cmd
}

func runEmailEvents(ctx context.Context, opts *Options, f *emailEventsFlags, input string) error {
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}
	id, err := resolveEmailID(ctx, apiClient, input)
	if err != nil {
		return err
	}

	// Single-page mode: one request; caller paginates with --cursor manually.
	if !f.all {
		page, err := fetchEmailEventsPage(ctx, apiClient, id, f.limit, f.cursor)
		if err != nil {
			return err
		}
		return printEmailEvents(opts, page)
	}

	cursor := f.cursor
	var allItems []client.EmailEventResponse
	warned := false
	for {
		page, err := fetchEmailEventsPage(ctx, apiClient, id, f.limit, cursor)
		if err != nil {
			return err
		}
		allItems = append(allItems, page.Items...)
		if !warned && len(allItems) > 1000 {
			fmt.Fprintf(opts.Stderr, "warning: walked %d events so far; ctrl-C to stop\n", len(allItems))
			warned = true
		}
		if page.NextCursor == nil || *page.NextCursor == "" {
			break
		}
		cursor = *page.NextCursor
	}

	return printEmailEvents(opts, &client.EmailEventListResponse{Items: allItems})
}

func fetchEmailEventsPage(
	ctx context.Context, apiClient *client.ClientWithResponses, id uuid.UUID, limit int, cursor string,
) (*client.EmailEventListResponse, error) {
	params := &client.ListEmailEventsEmailsEmailIdEventsGetParams{Limit: &limit}
	if cursor != "" {
		params.Cursor = &cursor
	}
	resp, err := apiClient.ListEmailEventsEmailsEmailIdEventsGetWithResponse(ctx, id, params)
	if err != nil {
		return nil, fmt.Errorf("email API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != 200 || resp.JSON200 == nil {
		return nil, apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}
	return resp.JSON200, nil
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
	if err := w.Flush(); err != nil {
		return err
	}
	if body.NextCursor != nil && *body.NextCursor != "" {
		fmt.Fprintf(opts.Stdout, "\nmore: --cursor %s\n", *body.NextCursor)
	}
	return nil
}
