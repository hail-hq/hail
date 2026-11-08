package cmd

import (
	"fmt"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/hail-hq/hail/cli/internal/client"
)

// newEmailSuppressionsCmd builds the `email suppressions` subcommand tree:
// list/delete against the org's email suppression list (unsubscribed
// addresses).
func newEmailSuppressionsCmd(opts *Options) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "suppressions",
		Short: "Manage the email suppression list",
	}
	cmd.AddCommand(newEmailSuppressionsListCmd(opts))
	cmd.AddCommand(newEmailSuppressionsDeleteCmd(opts))
	return cmd
}

func newEmailSuppressionsListCmd(opts *Options) *cobra.Command {
	var (
		limit  int
		cursor string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List suppressed email addresses (cursor-paginated)",
		Args:  argsOrHelp(0, ""),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			resp, err := apiClient.ListEmailSuppressionsV1EmailsSuppressionsGetWithResponse(ctx, &client.ListEmailSuppressionsV1EmailsSuppressionsGetParams{
				Limit:  &limit,
				Cursor: strPtr(cursor),
			})
			if err != nil {
				return fmt.Errorf("email suppressions API: %w", err)
			}
			if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
				return apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			return printSuppressionList(opts, resp.JSON200)
		},
	}
	cmd.Flags().IntVar(&limit, "limit", 50, "Page size (1..200)")
	cmd.Flags().StringVar(&cursor, "cursor", "", "Resume from a previous next_cursor")
	return cmd
}

func newEmailSuppressionsDeleteCmd(opts *Options) *cobra.Command {
	return &cobra.Command{
		Use:   "delete <address>",
		Short: "Remove an address from the suppression list (manual correction only)",
		Args:  argsOrHelp(1, "<address>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			resp, err := apiClient.DeleteEmailSuppressionV1EmailsSuppressionsAddressDeleteWithResponse(ctx, args[0], &client.DeleteEmailSuppressionV1EmailsSuppressionsAddressDeleteParams{})
			if err != nil {
				return fmt.Errorf("email suppressions API: %w", err)
			}
			if resp.HTTPResponse.StatusCode != http.StatusNoContent {
				return apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			fmt.Fprintf(opts.Stdout, "Removed %s from the suppression list.\n", args[0])
			return nil
		},
	}
}
