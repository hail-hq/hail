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

type emailListFlags struct {
	limit     int
	status    string
	direction string
	cursor    string
	all       bool
}

func newEmailListCmd(opts *Options) *cobra.Command {
	f := &emailListFlags{}
	cmd := &cobra.Command{
		Use:     "list",
		Aliases: []string{"ls"},
		Short:   "List recent emails (cursor-paginated)",
		Args:    cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runEmailList(cmd.Context(), opts, f)
		},
	}
	cmd.Flags().IntVar(&f.limit, "limit", 50, "Page size (1..200)")
	cmd.Flags().StringVar(&f.status, "status", "", "Filter by status (queued|sent|failed|bounced|complained|received)")
	cmd.Flags().StringVar(&f.direction, "direction", "", "Filter by direction (inbound|outbound)")
	cmd.Flags().StringVar(&f.cursor, "cursor", "", "Resume from a previous next_cursor")
	cmd.Flags().BoolVar(&f.all, "all", false, "Walk every page (warns at >1000 emails)")
	return cmd
}

func runEmailList(ctx context.Context, opts *Options, f *emailListFlags) error {
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}

	items, next, err := walkCursor(f.all, f.cursor, opts.Stderr, "emails",
		func(cursor string) (cursorPage[client.EmailSummary], error) {
			params := &client.ListEmailsV1EmailsGetParams{
				Limit:  &f.limit,
				Cursor: strPtr(cursor),
			}
			if f.status != "" {
				s := client.ListEmailsV1EmailsGetParamsStatus(f.status)
				params.Status = &s
			}
			if f.direction != "" {
				d := client.ListEmailsV1EmailsGetParamsDirection(f.direction)
				params.Direction = &d
			}
			resp, err := apiClient.ListEmailsV1EmailsGetWithResponse(ctx, params)
			if err != nil {
				return cursorPage[client.EmailSummary]{}, fmt.Errorf("email API: %w", err)
			}
			if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
				return cursorPage[client.EmailSummary]{}, apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			return cursorPage[client.EmailSummary]{items: resp.JSON200.Items, nextCursor: resp.JSON200.NextCursor}, nil
		})
	if err != nil {
		return err
	}
	return printEmailList(opts, &client.EmailListResponse{Items: items, NextCursor: next})
}

func printEmailList(opts *Options, body *client.EmailListResponse) error {
	if opts.JSON {
		return printJSON(opts.Stdout, body)
	}
	if len(body.Items) == 0 {
		fmt.Fprintln(opts.Stdout, "(no emails)")
		return nil
	}

	tw := tabwriter.NewWriter(opts.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(tw, "ID\tDIRECTION\tFROM\tTO\tSTATUS\tSUBJECT\tREQUESTED")
	for _, e := range body.Items {
		to := ""
		if len(e.ToAddresses) > 0 {
			to = e.ToAddresses[0]
			if len(e.ToAddresses) > 1 {
				to = fmt.Sprintf("%s (+%d)", to, len(e.ToAddresses)-1)
			}
		}
		subj := e.Subject
		if len(subj) > 40 {
			subj = subj[:37] + "..."
		}
		direction := "outbound"
		if e.Direction != nil {
			direction = string(*e.Direction)
		}
		fmt.Fprintf(
			tw, "%s\t%s\t%s\t%s\t%s\t%s\t%s\n",
			shortCallID(e.Id),
			direction,
			e.FromAddress,
			to,
			string(e.Status),
			subj,
			e.RequestedAt.UTC().Format(utcTSLayout),
		)
	}
	if err := tw.Flush(); err != nil {
		return fmt.Errorf("write table: %w", err)
	}
	if body.NextCursor != nil && *body.NextCursor != "" {
		fmt.Fprintf(opts.Stdout, "\nmore: --cursor %s\n", strings.TrimSpace(*body.NextCursor))
	}
	return nil
}
