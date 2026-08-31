package cmd

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"

	"github.com/google/uuid"
	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/hail-hq/hail/cli/internal/client"
)

// emailSendFlags are the values bound by `hail email send` — kept as a struct
// so tests can poke individual fields without leaking cobra wiring.
type emailSendFlags struct {
	consentFlags
	to             []string
	cc             []string
	bcc            []string
	from           string
	fromName       string
	replyTo        string
	subject        string
	body           string
	bodyHTML       string
	bodyFile       string
	bodyHTMLFile   string
	idempotencyKey string
	attach         []string
	attachIDs      []string
}

// newEmailCmd builds the `email` command tree.
//
// `hail email` on its own prints help; verbs and sub-trees hang off it.
// Mirrors `hail call`'s shape: `send` / `list` / `get` are the per-row
// verbs; `domain` is the identity-management sub-tree.
func newEmailCmd(opts *Options) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "email",
		Short: "Email operations (inbound + outbound)",
		Long: `hail email — email operations (inbound + outbound).

Inbound MIME attachments and the raw RFC 5322 source are exposed via
` + "`hail email attachment`" + ` and ` + "`hail email raw`" + `; ` + "`hail email tail`" + ` streams
events for a single message live, ` + "`hail email events`" + ` shows the
same timeline already-happened, and ` + "`hail email stats`" + ` rolls
deliverability up across your whole account.`,
	}
	cmd.AddCommand(newEmailSendCmd(opts))
	cmd.AddCommand(newEmailListCmd(opts))
	cmd.AddCommand(newEmailGetCmd(opts))
	cmd.AddCommand(newEmailTailCmd(opts))
	cmd.AddCommand(newEmailEventsCmd(opts))
	cmd.AddCommand(newEmailStatsCmd(opts))
	cmd.AddCommand(newEmailRawCmd(opts))
	cmd.AddCommand(newEmailAttachmentCmd(opts))
	cmd.AddCommand(newEmailAttachmentUploadCmd(opts))
	cmd.AddCommand(newEmailDomainCmd(opts))
	return cmd
}

func newEmailSendCmd(opts *Options) *cobra.Command {
	f := &emailSendFlags{}

	cmd := &cobra.Command{
		Use:   "send",
		Short: "Send an outbound email",
		Long: `hail email send — send one outbound email.

The recipient flag may be repeated, or comma-separated:
  --to alice@example.com --to bob@example.com
  --to alice@example.com,bob@example.com

Either --body or --body-html (or both) must be supplied. Prefer
--body-html: open and click tracking only works for emails with an
HTML body. A plain-text-only email still gets sent, delivered, and
bounce events, but opens and clicks are never tracked. --body-file
and --body-html-file read content from disk. --attach uploads a local
file and attaches it (repeatable); --attach-id attaches a file already
uploaded via ` + "`hail email attachment-upload`" + ` (repeatable).

--from is optional only while your org has one verified sender. With
several, omitting it fails with a 422 listing them — run
` + "`hail email domain list`" + ` and pass one. With none, the API
auto-mints a hail-mail address if the operator configured one.
See docs/public/self-host/aws-ses.md.

Example (minimal):
  hail email send --to alice@example.com --subject "Hi" --body "Hello" --recipient-consent`,
		Args:    cobra.NoArgs,
		PreRunE: requireMarkedFlags,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runEmailSend(cmd.Context(), cmd, opts, f)
		},
	}

	cmd.Flags().StringSliceVar(&f.to, "to", nil, "Recipient(s); repeat or comma-separate")
	cmd.Flags().StringSliceVar(&f.cc, "cc", nil, "CC recipient(s)")
	cmd.Flags().StringSliceVar(&f.bcc, "bcc", nil, "BCC recipient(s)")
	cmd.Flags().StringVar(&f.from, "from", "", "Override the from-address (must be a verified sender)")
	cmd.Flags().StringVar(&f.fromName, "from-name", "", "Display name for the From header (\"Acme Billing <billing@acme.com>\")")
	cmd.Flags().StringVar(&f.replyTo, "reply-to", "", "Reply-To header")
	cmd.Flags().StringVar(&f.subject, "subject", "", "Subject line")
	cmd.Flags().StringVar(&f.body, "body", "", "Plain-text body — one of --body/--body-html required")
	cmd.Flags().StringVar(&f.bodyHTML, "body-html", "", "HTML body — one of --body/--body-html required; needed for open/click tracking")
	cmd.Flags().StringVar(&f.bodyFile, "body-file", "", "Read plain-text body from this file (use '-' for stdin); exclusive with --body")
	cmd.Flags().StringVar(&f.bodyHTMLFile, "body-html-file", "", "Read HTML body from this file (use '-' for stdin); exclusive with --body-html")
	cmd.Flags().StringVar(&f.idempotencyKey, "idempotency-key", "", "Defaults to a fresh UUID")
	cmd.Flags().StringArrayVar(&f.attach, "attach", nil, "Local file path to upload and attach (repeatable)")
	cmd.Flags().StringArrayVar(&f.attachIDs, "attach-id", nil, "Pre-uploaded attachment id from `hail email attachment-upload` (repeatable)")
	f.registerConsentFlags(cmd, "email")
	cmd.MarkFlagRequired("to")
	cmd.MarkFlagRequired("subject")
	markOneOfRequired(cmd, "body", "body", "body-html", "body-file", "body-html-file")

	return cmd
}

func runEmailSend(ctx context.Context, cmd *cobra.Command, opts *Options, f *emailSendFlags) error {
	consentObtainedAt, err := f.validateConsent(cmd)
	if err != nil {
		return err
	}
	if f.subject == "" {
		return requireInputs(cmd, "--subject")
	}
	to, err := normalizeRecipients(f.to)
	if err != nil {
		return err
	}
	if len(to) == 0 {
		return requireInputs(cmd, "--to")
	}

	bodyText, err := resolveBody(f.body, f.bodyFile, opts)
	if err != nil {
		return fmt.Errorf("--body: %w", err)
	}
	bodyHTML, err := resolveBody(f.bodyHTML, f.bodyHTMLFile, opts)
	if err != nil {
		return fmt.Errorf("--body-html: %w", err)
	}
	if bodyText == "" && bodyHTML == "" {
		return requireInputs(cmd, "--body or --body-html or --body-file or --body-html-file")
	}

	cc, err := normalizeRecipients(f.cc)
	if err != nil {
		return fmt.Errorf("--cc: %w", err)
	}
	bcc, err := normalizeRecipients(f.bcc)
	if err != nil {
		return fmt.Errorf("--bcc: %w", err)
	}

	apiClient, err := opts.newClientWithIdempotency(f.idempotencyKey)
	if err != nil {
		return err
	}

	body := client.EmailCreate{
		To:       to,
		Subject:  f.subject,
		From:     strPtr(f.from),
		FromName: strPtr(f.fromName),
		ReplyTo:  strPtr(f.replyTo),
		BodyText: strPtr(bodyText),
		BodyHtml: strPtr(bodyHTML),
	}
	if len(cc) > 0 {
		body.Cc = &cc
	}
	if len(bcc) > 0 {
		body.Bcc = &bcc
	}

	body.RecipientConsent = f.recipientConsent
	if f.consentSource != "" {
		body.ConsentSource = strPtr(f.consentSource)
	}
	body.ConsentObtainedAt = consentObtainedAt
	if f.messageType != "" {
		mt := client.EmailCreateMessageType(f.messageType)
		body.MessageType = &mt
	}

	var attachmentIDs []string
	attachmentIDs = append(attachmentIDs, f.attachIDs...)
	for _, path := range f.attach {
		data, err := os.ReadFile(path)
		if err != nil {
			return fmt.Errorf("--attach %s: %w", path, err)
		}
		att, err := uploadEmailAttachment(ctx, apiClient, path, data)
		if err != nil {
			return fmt.Errorf("--attach %s: %w", path, err)
		}
		attachmentIDs = append(attachmentIDs, att.Id.String())
	}
	if len(attachmentIDs) > 0 {
		ids := make([]openapi_types.UUID, 0, len(attachmentIDs))
		for _, s := range attachmentIDs {
			id, err := uuid.Parse(s)
			if err != nil {
				return fmt.Errorf("--attach-id %q: not a valid UUID: %w", s, err)
			}
			ids = append(ids, openapi_types.UUID(id))
		}
		body.AttachmentIds = &ids
	}

	// Deliberately the raw (non-WithResponse) client method here, not
	// CreateEmailV1EmailsPostWithResponse: its generated parser eagerly
	// unmarshals ANY non-2xx JSON body into the OpenAPI-declared
	// HTTPValidationError shape ({"detail": []ValidationError}) and
	// discards the raw body if that unmarshal fails — which it does for
	// any of this route's plain-string-detail 422s (the attachment
	// size-cap rejection, "not a verified sender", "pending DKIM
	// verification", etc.), surfacing a raw Go json.Unmarshal error
	// instead of the real message. Parsing the response ourselves keeps
	// the real body available for apiError's safe fallback.
	httpResp, err := apiClient.CreateEmailV1EmailsPost(
		ctx, &client.CreateEmailV1EmailsPostParams{}, body,
	)
	if err != nil {
		return fmt.Errorf("email API: %w", err)
	}
	defer httpResp.Body.Close()
	respBody, err := io.ReadAll(httpResp.Body)
	if err != nil {
		return fmt.Errorf("email API: read response: %w", err)
	}
	if httpResp.StatusCode != http.StatusCreated {
		return apiError(httpResp.StatusCode, respBody)
	}
	var email client.EmailResponse
	if err := json.Unmarshal(respBody, &email); err != nil {
		return fmt.Errorf("email API: parse response: %w", err)
	}

	return printEmail(opts, &email)
}

// normalizeRecipients trims whitespace from each entry and drops empty
// strings — guards against the `--to "" --to a@b.com` shape that flags
// like cobra's StringSliceVar will happily accept.
func normalizeRecipients(in []string) ([]string, error) {
	out := make([]string, 0, len(in))
	for _, raw := range in {
		s := strings.TrimSpace(raw)
		if s == "" {
			continue
		}
		out = append(out, s)
	}
	return out, nil
}

// resolveBody returns the explicit string, or the contents of the file
// (with '-' meaning stdin). Returns "" when neither is set so callers
// can compose `body_text` and `body_html` independently.
func resolveBody(literal, path string, opts *Options) (string, error) {
	if literal != "" && path != "" {
		return "", fmt.Errorf("supply only one of literal flag or file flag")
	}
	if literal != "" {
		return literal, nil
	}
	if path == "" {
		return "", nil
	}
	if path == "-" {
		buf, err := readAll(os.Stdin)
		if err != nil {
			return "", fmt.Errorf("read stdin: %w", err)
		}
		return buf, nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return "", fmt.Errorf("read %s: %w", path, err)
	}
	return string(data), nil
}

// readAll exists as a small indirection so future test seams can swap
// stdin without rewriting resolveBody. Trivial today.
func readAll(r interface{ Read(p []byte) (int, error) }) (string, error) {
	const chunk = 4096
	var buf []byte
	tmp := make([]byte, chunk)
	for {
		n, err := r.Read(tmp)
		if n > 0 {
			buf = append(buf, tmp[:n]...)
		}
		if err != nil {
			if err.Error() == "EOF" {
				break
			}
			return "", err
		}
	}
	return string(buf), nil
}

// printEmail renders the success response — JSON when --json, otherwise
// a short human form mirroring `hail call`'s output style.
func printEmail(opts *Options, email *client.EmailResponse) error {
	if opts.JSON {
		return printJSON(opts.Stdout, email)
	}

	fmt.Fprintf(opts.Stdout, "✓ Email %s: %s\n", string(email.Status), email.Id.String())
	fmt.Fprintf(opts.Stdout, "  From:    %s\n", email.FromAddress)
	fmt.Fprintf(opts.Stdout, "  To:      %s\n", strings.Join(email.ToAddresses, ", "))
	fmt.Fprintf(opts.Stdout, "  Subject: %s\n", email.Subject)
	if email.ProviderMessageId != nil {
		fmt.Fprintf(opts.Stdout, "  SES Id:  %s\n", *email.ProviderMessageId)
	}
	return nil
}
