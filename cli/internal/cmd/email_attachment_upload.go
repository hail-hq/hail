package cmd

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
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
garbage-collects it (24h if never referenced by a send). Files over 25MB
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

	// Deliberately the raw (non-WithResponse) client method here, not
	// UploadEmailAttachmentWithBodyWithResponse: its generated parser
	// (ParseUploadEmailAttachmentResponse) eagerly unmarshals ANY non-2xx
	// JSON body into the OpenAPI-declared HTTPValidationError shape
	// ({"detail": []ValidationError}) and discards the raw body if that
	// unmarshal fails — which it always does for this endpoint's size-cap
	// rejection, since that returns a plain string detail
	// ({"detail": "..."}) instead. That mismatch surfaced as a raw Go
	// `json: cannot unmarshal string into ... []client.ValidationError`
	// error instead of the actual "too large" message. Parsing the
	// response ourselves keeps the real body available for apiError's
	// safe (already-handles-both-shapes) fallback.
	//
	// UploadEmailAttachmentWithBody takes an extra *params argument beyond
	// what the task brief predicted — oapi-codegen v2 generates one for
	// every route carrying the shared optional `authorization` header
	// parameter (see UploadEmailAttachmentParams), the same shape other
	// routes take elsewhere in this package. Real auth goes through the
	// request-editor-injected Authorization header on the client, so this
	// is always the zero value.
	httpResp, err := apiClient.UploadEmailAttachmentWithBody(
		ctx, &client.UploadEmailAttachmentParams{}, w.FormDataContentType(), &buf,
	)
	if err != nil {
		return nil, fmt.Errorf("attachment upload API: %w", err)
	}
	defer httpResp.Body.Close()
	body, err := io.ReadAll(httpResp.Body)
	if err != nil {
		return nil, fmt.Errorf("attachment upload API: read response: %w", err)
	}
	if httpResp.StatusCode != http.StatusCreated {
		return nil, apiError(httpResp.StatusCode, body)
	}
	var att client.EmailAttachmentUploadResponse
	if err := json.Unmarshal(body, &att); err != nil {
		return nil, fmt.Errorf("attachment upload API: parse response: %w", err)
	}
	return &att, nil
}
