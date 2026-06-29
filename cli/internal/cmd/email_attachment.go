package cmd

import (
	"context"
	"fmt"
	"io"
	"net/http"

	"github.com/google/uuid"
	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/hail-hq/hail/cli/internal/client"
)

func newEmailAttachmentCmd(opts *Options) *cobra.Command {
	var output string
	cmd := &cobra.Command{
		Use:   "attachment <email-id> <attachment-id>",
		Short: "Download one attachment as raw bytes",
		Long: `hail email attachment — fetch a single attachment.

By default writes to stdout (binary stream). Use --output path to write
to a file. If stdout is a TTY and no --output is set, the command
refuses to write so you don't print binary garbage to your terminal.

<email-id> and <attachment-id> may each be a full UUID or a 4+ char
hex prefix. Attachment prefix is resolved within the parent email's
attachment list (no list endpoint exists for attachments alone). When
the attachment id is a full UUID, the parent fetch is skipped.

--json is not supported (binary stream).`,
		Args: argsOrHelp(2, "<email-id> <attachment-id>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			if opts.JSON {
				return helpAndFail(cmd, "--json is not supported on attachment — output is a binary stream")
			}
			return runEmailAttachment(cmd.Context(), cmd, opts, args[0], args[1], output)
		},
	}
	cmd.Flags().StringVar(&output, "output", "", "Write to a file (use '-' or omit for stdout)")
	return cmd
}

func runEmailAttachment(ctx context.Context, cmd *cobra.Command, opts *Options, emailInput, attachInput, output string) error {
	// Refuse to write binary to a TTY before any I/O so the network hop is
	// not wasted on an invalid setup.
	if (output == "" || output == "-") && isTTY(opts.Stdout) {
		return helpAndFail(cmd, "stdout is a TTY; pass --output <path> or pipe to a file")
	}

	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}

	emailID, err := resolveEmailID(ctx, apiClient, emailInput)
	if err != nil {
		return err
	}

	attachID, err := resolveAttachmentID(ctx, apiClient, emailID, attachInput)
	if err != nil {
		return err
	}

	resp, err := apiClient.GetEmailAttachmentEmailsEmailIdAttachmentsAttachmentIdGet(
		ctx, openapi_types.UUID(emailID), openapi_types.UUID(attachID),
		&client.GetEmailAttachmentEmailsEmailIdAttachmentsAttachmentIdGetParams{},
	)
	if err != nil {
		return fmt.Errorf("attachment API: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, errorBodyLimit))
		return apiError(resp.StatusCode, body)
	}

	dst, closeDst, err := openOutput(opts, output)
	if err != nil {
		return err
	}
	if _, err := io.Copy(dst, resp.Body); err != nil {
		_ = closeDst()
		return fmt.Errorf("write attachment: %w", err)
	}
	if err := closeDst(); err != nil {
		return fmt.Errorf("close %s: %w", output, err)
	}
	return nil
}

// resolveAttachmentID resolves an attachment id (full UUID or 4+ char hex
// prefix) within the parent email. Full UUIDs short-circuit — we let the
// attachment endpoint 404 if the id does not belong to this email,
// avoiding an extra GET. Prefix inputs require the parent's attachment
// list since there is no separate attachments list endpoint.
func resolveAttachmentID(ctx context.Context, apiClient *client.ClientWithResponses, emailID uuid.UUID, input string) (uuid.UUID, error) {
	if parsed, err := uuid.Parse(input); err == nil {
		return parsed, nil
	}

	parent, err := apiClient.GetEmailEmailsEmailIdGetWithResponse(
		ctx, openapi_types.UUID(emailID), &client.GetEmailEmailsEmailIdGetParams{},
	)
	if err != nil {
		return uuid.Nil, fmt.Errorf("email API: %w", err)
	}
	if parent.HTTPResponse.StatusCode != http.StatusOK || parent.JSON200 == nil {
		return uuid.Nil, apiError(parent.HTTPResponse.StatusCode, parent.Body)
	}
	atts := parent.JSON200.Attachments
	if atts == nil || len(*atts) == 0 {
		return uuid.Nil, fmt.Errorf("email has no attachments")
	}
	id, _, err := matchHexPrefix(*atts, func(a client.EmailAttachmentResponse) openapi_types.UUID { return a.Id }, input, "attachment")
	return id, err
}
