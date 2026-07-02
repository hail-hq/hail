package cmd

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"text/tabwriter"

	"github.com/spf13/cobra"

	"github.com/hail-hq/hail/cli/internal/client"
)

type callListFlags struct {
	limit  int
	status string
	to     string
	cursor string
	all    bool
}

func newCallListCmd(opts *Options) *cobra.Command {
	f := &callListFlags{}
	cmd := &cobra.Command{
		Use:     "list",
		Aliases: []string{"ls"},
		Short:   "List recent calls (cursor-paginated)",
		Args:    cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runCallList(cmd.Context(), opts, f)
		},
	}
	cmd.Flags().IntVar(&f.limit, "limit", 50, "Page size (1..200)")
	cmd.Flags().StringVar(&f.status, "status", "", "Filter by status (queued|dialing|ringing|in_progress|completed|failed|busy|no_answer|canceled)")
	cmd.Flags().StringVar(&f.to, "to", "", "Filter by destination E.164 number")
	cmd.Flags().StringVar(&f.cursor, "cursor", "", "Resume from a previous next_cursor")
	cmd.Flags().BoolVar(&f.all, "all", false, "Walk every page (warns at >1000 calls)")
	return cmd
}

func runCallList(ctx context.Context, opts *Options, f *callListFlags) error {
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}

	items, next, err := walkCursor(f.all, f.cursor, opts.Stderr, "calls",
		func(cursor string) (cursorPage[client.CallResponse], error) {
			params := &client.ListCallsCallsGetParams{
				Limit:  &f.limit,
				Cursor: strPtr(cursor),
				To:     strPtr(f.to),
			}
			if f.status != "" {
				s := client.ListCallsCallsGetParamsStatus(f.status)
				params.Status = &s
			}
			resp, err := apiClient.ListCallsCallsGetWithResponse(ctx, params)
			if err != nil {
				return cursorPage[client.CallResponse]{}, fmt.Errorf("call API: %w", err)
			}
			if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
				return cursorPage[client.CallResponse]{}, apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			return cursorPage[client.CallResponse]{items: resp.JSON200.Items, nextCursor: resp.JSON200.NextCursor}, nil
		})
	if err != nil {
		return err
	}
	return printCallList(opts, &client.CallListResponse{Items: items, NextCursor: next})
}

// printCallList prints a CallListResponse: JSON or a table.
func printCallList(opts *Options, body *client.CallListResponse) error {
	if opts.JSON {
		return printJSON(opts.Stdout, body)
	}

	if len(body.Items) == 0 {
		fmt.Fprintln(opts.Stdout, "(no calls)")
		return nil
	}

	tw := tabwriter.NewWriter(opts.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(tw, "ID\tTO\tSTATUS\tREQUESTED")
	for _, c := range body.Items {
		fmt.Fprintf(tw, "%s\t%s\t%s\t%s\n", shortCallID(c.Id), c.ToE164, string(c.Status), c.RequestedAt.UTC().Format(utcTSLayout))
	}
	if err := tw.Flush(); err != nil {
		return fmt.Errorf("write table: %w", err)
	}
	if body.NextCursor != nil && *body.NextCursor != "" {
		fmt.Fprintf(opts.Stdout, "\nmore: --cursor %s\n", strings.TrimSpace(*body.NextCursor))
	}
	return nil
}
