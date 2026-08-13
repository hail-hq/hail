package cmd

import (
	"context"
	"fmt"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/hail-hq/hail/cli/internal/client"
)

// newWhoamiCmd builds `hail whoami`.
//
// `hail auth token` answers "which key am I using"; this answers "who does
// that key belong to" — the org it bills to and the human it was issued
// for. Scripts use the email to address mail as that person, e.g. as the
// Reply-To on an agent send.
func newWhoamiCmd(opts *Options) *cobra.Command {
	return &cobra.Command{
		Use:   "whoami",
		Short: "Show the org and user behind the current API key",
		Long: `hail whoami — identify the caller behind the resolved API key.

Prints the organization id, the user the key belongs to, and how the
request authenticated. A shared operator key (HAIL_API_KEY on a
self-hosted server) carries no user, so those fields come back empty.`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runWhoami(cmd.Context(), opts)
		},
	}
}

func runWhoami(ctx context.Context, opts *Options) error {
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}
	resp, err := apiClient.GetWhoamiWhoamiGetWithResponse(
		ctx, &client.GetWhoamiWhoamiGetParams{},
	)
	if err != nil {
		return fmt.Errorf("whoami API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}
	return printWhoami(opts, resp.JSON200)
}

func printWhoami(opts *Options, body *client.WhoamiResponse) error {
	if opts.JSON {
		return printJSON(opts.Stdout, body)
	}
	switch {
	case body.Email != nil && *body.Email != "":
		fmt.Fprintf(opts.Stdout, "%s\n", *body.Email)
	case body.AuthKind == client.Shared:
		fmt.Fprintln(opts.Stdout, "(no user — shared operator key)")
	default:
		// An api-key/JWT principal whose users row is gone: the key still
		// identifies an org and a user id, just no mailbox. Saying "shared
		// operator key" here would name the wrong auth path.
		fmt.Fprintln(opts.Stdout, "(no user email on record)")
	}
	if body.Name != nil && *body.Name != "" {
		fmt.Fprintf(opts.Stdout, "  Name:         %s\n", *body.Name)
	}
	if body.UserId != nil {
		fmt.Fprintf(opts.Stdout, "  User:         %s\n", body.UserId.String())
	}
	fmt.Fprintf(opts.Stdout, "  Organization: %s\n", body.OrganizationId.String())
	fmt.Fprintf(opts.Stdout, "  Auth:         %s\n", string(body.AuthKind))
	return nil
}
