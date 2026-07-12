package cmd

import (
	"bytes"
	"context"
	"fmt"
	"mime/multipart"
	"net/http"
	"os"
	"path/filepath"

	"github.com/spf13/cobra"

	"github.com/hail-hq/hail/cli/internal/client"
)

func newEmailAttachmentUploadCmd(opts *Options) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "attachment-upload <file>",
		Short: "Upload a file for use as an outbound email attachment",
		Long: `hail email attachment-upload — upload a local file, get back a
reusable attachment id.

Pass the returned id to ` + "`hail email send --attach-id <id>`" + ` (or,
simpler, use ` + "`hail email send --attach <file>`" + ` to upload and send
in one step). The id can be reused across many sends until Hail
garbage-collects it (24h if never referenced by a send). Files over 10MB
(combined with the message body and any other attachments, per send) are
rejected — host large files externally and link to them in the body
instead.`,
		Args: argsOrHelp(1, "<file>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runEmailAttachmentUpload(cmd.Context(), opts, args[0])
		},
	}
	return cmd
}

func runEmailAttachmentUpload(ctx context.Context, opts *Options, path string) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("read %s: %w", path, err)
	}
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}
	att, err := uploadEmailAttachment(ctx, apiClient, path, data)
	if err != nil {
		return err
	}
	if opts.JSON {
		return printJSON(opts.Stdout, att)
	}
	fmt.Fprintf(opts.Stdout, "✓ Attachment uploaded: %s\n", att.Id.String())
	fmt.Fprintf(opts.Stdout, "  Filename: %s\n", att.Filename)
	fmt.Fprintf(opts.Stdout, "  Size:     %d bytes\n", att.SizeBytes)
	return nil
}

// uploadEmailAttachment builds a multipart body from raw file bytes and
// posts it to /email-attachments. Shared by `attachment-upload` and
// `email send --attach`.
func uploadEmailAttachment(
	ctx context.Context, apiClient *client.ClientWithResponses, path string, data []byte,
) (*client.EmailAttachmentUploadResponse, error) {
	var buf bytes.Buffer
	w := multipart.NewWriter(&buf)
	part, err := w.CreateFormFile("file", filepath.Base(path))
	if err != nil {
		return nil, fmt.Errorf("build upload: %w", err)
	}
	if _, err := part.Write(data); err != nil {
		return nil, fmt.Errorf("build upload: %w", err)
	}
	if err := w.Close(); err != nil {
		return nil, fmt.Errorf("build upload: %w", err)
	}

	// UploadEmailAttachmentWithBodyWithResponse takes an extra *params
	// argument beyond what the task brief predicted — oapi-codegen v2
	// generates one for every route carrying the shared optional
	// `authorization` header parameter (see UploadEmailAttachmentParams),
	// the same shape CreateCallCallsPostWithResponse and
	// CreateEmailEmailsPostWithResponse already take elsewhere in this
	// package. Real auth goes through the request-editor-injected
	// Authorization header on the client, so this is always the zero value.
	resp, err := apiClient.UploadEmailAttachmentWithBodyWithResponse(
		ctx, &client.UploadEmailAttachmentParams{}, w.FormDataContentType(), &buf,
	)
	if err != nil {
		return nil, fmt.Errorf("attachment upload API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusCreated || resp.JSON201 == nil {
		return nil, apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}
	return resp.JSON201, nil
}
