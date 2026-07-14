package cmd

import (
	"context"
	"fmt"
	"net/http"
	"text/tabwriter"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/hail-hq/hail/cli/internal/client"
)

// smsFlags are the values bound by `hail sms` — kept as a struct so the
// test suite can poke individual fields without leaking cobra wiring.
type smsFlags struct {
	consentFlags
	body           string
	from           string
	idempotencyKey string
}

// newSmsCmd builds the `sms` command tree.
//
// Mirrors `hail call`'s shape: the parent itself takes a phone-number
// positional argument (`hail sms +1...`) and hosts sibling subcommands
// `status` and `list`.
func newSmsCmd(opts *Options) *cobra.Command {
	f := &smsFlags{}

	cmd := &cobra.Command{
		Use:   "sms <to-number>",
		Short: "Send an outbound SMS (or use a subcommand)",
		Long: `hail sms — send an outbound text message.

Requires a dedicated phone number on your organization — SMS does not
use the shared voice pool.

Example (minimal):
  hail sms +15551234567 --body "Hello!" --recipient-consent`,
		Args:    argsOrHelp(1, "<to-number>"),
		PreRunE: requireMarkedFlags,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runSms(cmd.Context(), cmd, opts, f, args[0])
		},
	}

	cmd.Flags().StringVar(&f.body, "body", "", "Message text (required)")
	cmd.Flags().StringVar(&f.from, "from", "", "Override the from-number (default: the org's dedicated number)")
	cmd.Flags().StringVar(&f.idempotencyKey, "idempotency-key", "", "Defaults to a fresh UUID")
	f.registerConsentFlags(cmd, "text")
	cmd.MarkFlagRequired("body")

	cmd.AddCommand(newSmsStatusCmd(opts))
	cmd.AddCommand(newSmsListCmd(opts))
	cmd.AddCommand(newSmsSuppressionsCmd(opts))

	return cmd
}

func runSms(ctx context.Context, cmd *cobra.Command, opts *Options, f *smsFlags, toNumber string) error {
	consentObtainedAt, err := f.validateConsent(cmd)
	if err != nil {
		return err
	}

	body := client.SmsCreate{
		To:   toNumber,
		Body: f.body,
		From: strPtr(f.from),
	}
	body.RecipientConsent = f.recipientConsent
	if f.consentSource != "" {
		body.ConsentSource = strPtr(f.consentSource)
	}
	body.ConsentObtainedAt = consentObtainedAt
	if f.messageType != "" {
		mt := client.SmsCreateMessageType(f.messageType)
		body.MessageType = &mt
	}

	apiClient, err := opts.newClientWithIdempotency(f.idempotencyKey)
	if err != nil {
		return err
	}

	resp, err := apiClient.CreateSmsSmsPostWithResponse(ctx, &client.CreateSmsSmsPostParams{}, body)
	if err != nil {
		return fmt.Errorf("sms API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusCreated || resp.JSON201 == nil {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}

	return printSms(opts, resp.JSON201, true)
}

// printSms renders an SmsResponse — JSON when --json, otherwise a short
// human form mirroring `hail call`'s output style. One renderer for both
// the just-sent view (justSent: ✓/✗ banner + track hint) and the `status`
// view (Requested/Sent/Provider fields) so the two can't drift.
func printSms(opts *Options, sms *client.SmsResponse, justSent bool) error {
	if opts.JSON {
		return printJSON(opts.Stdout, sms)
	}

	switch {
	case justSent && sms.Status == client.SmsResponseStatusFailed:
		fmt.Fprintf(opts.Stdout, "✗ SMS rejected: %s\n", sms.Id.String())
	case justSent:
		fmt.Fprintf(opts.Stdout, "✓ SMS sent: %s\n", sms.Id.String())
	default:
		fmt.Fprintf(opts.Stdout, "SMS:     %s\n", sms.Id.String())
	}
	fmt.Fprintf(opts.Stdout, "  From:      %s\n", sms.FromE164)
	fmt.Fprintf(opts.Stdout, "  To:        %s\n", sms.ToE164)
	fmt.Fprintf(opts.Stdout, "  Status:    %s\n", string(sms.Status))
	if !justSent {
		fmt.Fprintf(opts.Stdout, "  Requested: %s\n", sms.RequestedAt.UTC().Format(utcTSLayout))
		if sms.SentAt != nil {
			fmt.Fprintf(opts.Stdout, "  Sent:      %s\n", sms.SentAt.UTC().Format(utcTSLayout))
		}
	}
	if sms.ErrorCode != nil && *sms.ErrorCode != "" {
		fmt.Fprintf(opts.Stdout, "  Error:     %s\n", *sms.ErrorCode)
	}
	if !justSent && sms.ProviderMessageSid != nil && *sms.ProviderMessageSid != "" {
		fmt.Fprintf(opts.Stdout, "  Provider:  %s\n", *sms.ProviderMessageSid)
	}
	if justSent {
		fmt.Fprintf(opts.Stdout, "  Track:     hail sms status %s\n", sms.Id.String())
	}
	return nil
}

func newSmsStatusCmd(opts *Options) *cobra.Command {
	return &cobra.Command{
		Use:     "status <id>",
		Aliases: []string{"get"},
		Short:   "Fetch the current state of one SMS",
		Args:    argsOrHelp(1, "<id>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runSmsStatus(cmd.Context(), opts, args[0])
		},
	}
}

func runSmsStatus(ctx context.Context, opts *Options, idStr string) error {
	id, err := uuid.Parse(idStr)
	if err != nil {
		return fmt.Errorf("invalid sms id %q: %w", idStr, err)
	}

	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}

	resp, err := apiClient.GetSmsSmsSmsIdGetWithResponse(ctx, id, &client.GetSmsSmsSmsIdGetParams{})
	if err != nil {
		return fmt.Errorf("sms API: %w", err)
	}
	if resp.HTTPResponse.StatusCode == http.StatusNotFound {
		return fmt.Errorf("sms %s not found (or not in your org)", id.String())
	}
	if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}

	return printSms(opts, resp.JSON200, false)
}

type smsListFlags struct {
	limit  int
	status string
	to     string
	cursor string
}

func newSmsListCmd(opts *Options) *cobra.Command {
	f := &smsListFlags{}
	cmd := &cobra.Command{
		Use:     "list",
		Aliases: []string{"ls"},
		Short:   "List recent SMS messages (cursor-paginated)",
		Args:    cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runSmsList(cmd.Context(), opts, f)
		},
	}
	cmd.Flags().IntVar(&f.limit, "limit", 50, "Page size (1..200)")
	cmd.Flags().StringVar(&f.status, "status", "", "Filter by status (queued|sent|delivered|failed|undelivered|received)")
	cmd.Flags().StringVar(&f.to, "to", "", "Filter by destination E.164 number")
	cmd.Flags().StringVar(&f.cursor, "cursor", "", "Resume from a previous next_cursor")
	return cmd
}

func runSmsList(ctx context.Context, opts *Options, f *smsListFlags) error {
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}

	params := &client.ListSmsSmsGetParams{
		Limit:  &f.limit,
		Cursor: strPtr(f.cursor),
		To:     strPtr(f.to),
	}
	if f.status != "" {
		s := client.ListSmsSmsGetParamsStatus(f.status)
		params.Status = &s
	}

	resp, err := apiClient.ListSmsSmsGetWithResponse(ctx, params)
	if err != nil {
		return fmt.Errorf("sms API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}

	return printSmsList(opts, resp.JSON200)
}

// printSmsList prints an SmsListResponse: JSON or a table.
func printSmsList(opts *Options, body *client.SmsListResponse) error {
	if opts.JSON {
		return printJSON(opts.Stdout, body)
	}

	if len(body.Items) == 0 {
		fmt.Fprintln(opts.Stdout, "(no sms)")
		return nil
	}

	tw := tabwriter.NewWriter(opts.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(tw, "ID\tTO\tSTATUS\tREQUESTED")
	for _, s := range body.Items {
		fmt.Fprintf(tw, "%s\t%s\t%s\t%s\n", s.Id.String(), s.ToE164, string(s.Status), s.RequestedAt.UTC().Format(utcTSLayout))
	}
	if err := tw.Flush(); err != nil {
		return fmt.Errorf("write table: %w", err)
	}
	if body.NextCursor != nil && *body.NextCursor != "" {
		fmt.Fprintf(opts.Stdout, "\nmore: --cursor %s\n", *body.NextCursor)
	}
	return nil
}

// newSmsSuppressionsCmd builds the `sms suppressions` subcommand tree —
// list/delete against the opt-out (STOP/START) suppression list.
func newSmsSuppressionsCmd(opts *Options) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "suppressions",
		Short: "Manage the SMS opt-out (suppression) list",
	}
	cmd.AddCommand(newSmsSuppressionsListCmd(opts))
	cmd.AddCommand(newSmsSuppressionsDeleteCmd(opts))
	return cmd
}

func newSmsSuppressionsListCmd(opts *Options) *cobra.Command {
	var (
		limit  int
		cursor string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List opted-out numbers (cursor-paginated)",
		Args:  argsOrHelp(0, ""),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			resp, err := apiClient.ListSmsSuppressionsSmsSuppressionsGetWithResponse(ctx, &client.ListSmsSuppressionsSmsSuppressionsGetParams{
				Limit:  &limit,
				Cursor: strPtr(cursor),
			})
			if err != nil {
				return fmt.Errorf("sms suppressions API: %w", err)
			}
			if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
				return apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			if opts.JSON {
				return printJSON(opts.Stdout, resp.JSON200)
			}
			for _, s := range resp.JSON200.Items {
				fmt.Fprintf(opts.Stdout, "%s  %s  %s\n", s.Recipient, s.Reason, s.Source)
			}
			if resp.JSON200.NextCursor != nil && *resp.JSON200.NextCursor != "" {
				fmt.Fprintf(opts.Stdout, "\nmore: --cursor %s\n", *resp.JSON200.NextCursor)
			}
			return nil
		},
	}
	cmd.Flags().IntVar(&limit, "limit", 50, "Page size (1..200)")
	cmd.Flags().StringVar(&cursor, "cursor", "", "Resume from a previous next_cursor")
	return cmd
}

func newSmsSuppressionsDeleteCmd(opts *Options) *cobra.Command {
	return &cobra.Command{
		Use:   "delete <number>",
		Short: "Remove a number from the opt-out list (manual correction only)",
		Args:  argsOrHelp(1, "<number>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			resp, err := apiClient.DeleteSmsSuppressionSmsSuppressionsNumberDeleteWithResponse(ctx, args[0], &client.DeleteSmsSuppressionSmsSuppressionsNumberDeleteParams{})
			if err != nil {
				return fmt.Errorf("sms suppressions API: %w", err)
			}
			if resp.HTTPResponse.StatusCode != http.StatusNoContent {
				return apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			fmt.Fprintf(opts.Stdout, "Removed %s from the opt-out list.\n", args[0])
			return nil
		},
	}
}
