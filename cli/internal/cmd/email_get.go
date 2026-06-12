package cmd

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"text/tabwriter"

	"github.com/google/uuid"
	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/hail-hq/hail/cli/internal/client"
)

func newEmailGetCmd(opts *Options) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "get <id>",
		Short: "Fetch one email by id",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runEmailGet(cmd.Context(), opts, args[0])
		},
	}
	return cmd
}

func runEmailGet(ctx context.Context, opts *Options, idStr string) error {
	id, err := uuid.Parse(idStr)
	if err != nil {
		return fmt.Errorf("invalid email id: %w", err)
	}
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}
	resp, err := apiClient.GetEmailEmailsEmailIdGetWithResponse(
		ctx,
		openapi_types.UUID(id),
		&client.GetEmailEmailsEmailIdGetParams{},
	)
	if err != nil {
		return fmt.Errorf("email API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}
	return printEmailDetail(opts, resp.JSON200)
}

func printEmailDetail(opts *Options, email *client.EmailResponse) error {
	if opts.JSON {
		return printJSON(opts.Stdout, email)
	}
	tw := tabwriter.NewWriter(opts.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintf(tw, "ID:\t%s\n", email.Id.String())
	direction := "outbound"
	if email.Direction != nil {
		direction = string(*email.Direction)
	}
	fmt.Fprintf(tw, "Direction:\t%s\n", direction)
	fmt.Fprintf(tw, "Status:\t%s\n", string(email.Status))
	fmt.Fprintf(tw, "From:\t%s\n", email.FromAddress)
	if len(email.ToAddresses) > 0 {
		fmt.Fprintf(tw, "To:\t%s\n", strings.Join(email.ToAddresses, ", "))
	}
	if email.CcAddresses != nil && len(*email.CcAddresses) > 0 {
		fmt.Fprintf(tw, "Cc:\t%s\n", strings.Join(*email.CcAddresses, ", "))
	}
	if email.ReplyTo != nil && *email.ReplyTo != "" {
		fmt.Fprintf(tw, "Reply-To:\t%s\n", *email.ReplyTo)
	}
	fmt.Fprintf(tw, "Subject:\t%s\n", email.Subject)
	if email.MessageId != nil && *email.MessageId != "" {
		fmt.Fprintf(tw, "Message-ID:\t%s\n", *email.MessageId)
	}
	if email.InReplyTo != nil && *email.InReplyTo != "" {
		fmt.Fprintf(tw, "In-Reply-To:\t%s\n", *email.InReplyTo)
	}
	fmt.Fprintf(tw, "Requested:\t%s\n", email.RequestedAt.UTC().Format(utcTSLayout))
	if email.SentAt != nil {
		fmt.Fprintf(tw, "Sent:\t%s\n", email.SentAt.UTC().Format(utcTSLayout))
	}
	if email.ProviderReceivedAt != nil {
		fmt.Fprintf(tw, "Received:\t%s\n", email.ProviderReceivedAt.UTC().Format(utcTSLayout))
	}
	for _, v := range []struct {
		label string
		val   *string
	}{
		{"SPF", email.SpfVerdict},
		{"DKIM", email.DkimVerdict},
		{"DMARC", email.DmarcVerdict},
		{"Spam", email.SpamVerdict},
		{"Virus", email.VirusVerdict},
	} {
		if v.val != nil && *v.val != "" {
			fmt.Fprintf(tw, "%s verdict:\t%s\n", v.label, *v.val)
		}
	}
	if email.RawUrl != nil && *email.RawUrl != "" {
		fmt.Fprintf(tw, "Raw MIME:\t%s\n", *email.RawUrl)
	}
	if email.Attachments != nil && len(*email.Attachments) > 0 {
		fmt.Fprintf(tw, "Attachments:\t%d\n", len(*email.Attachments))
		for i, att := range *email.Attachments {
			fmt.Fprintf(tw, "  %d:\t%s (%s, %d bytes)\n", i+1, att.Filename, att.ContentType, att.SizeBytes)
		}
	}
	if err := tw.Flush(); err != nil {
		return fmt.Errorf("write detail: %w", err)
	}
	return nil
}
