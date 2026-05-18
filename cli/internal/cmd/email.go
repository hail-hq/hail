package cmd

import (
	"context"
	"fmt"
	"net/http"
	"os"
	"strings"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/hail-hq/hail/cli/internal/client"
)

// emailSendFlags are the values bound by `hail email send` — kept as a struct
// so tests can poke individual fields without leaking cobra wiring.
type emailSendFlags struct {
	to             []string
	cc             []string
	bcc            []string
	from           string
	replyTo        string
	subject        string
	body           string
	bodyHTML       string
	bodyFile       string
	bodyHTMLFile   string
	idempotencyKey string
}

// newEmailCmd builds the `email` command tree.
//
// `hail email` on its own prints help; verbs and sub-trees hang off it.
// Today: `send` for outbound, `sender-domain` for managing the SES
// identities mail is sent from. The shape mirrors `hail auth` (group
// of subcommands) rather than the dual-form `hail call`, since "email"
// alone doesn't have an obvious positional argument the way a phone
// number does for calls.
func newEmailCmd(opts *Options) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "email",
		Short: "Outbound email",
		Long: `hail email — outbound email operations.

Subcommands:
  hail email send                    Send a message through your configured sender domain.
  hail email sender-domain ...       Manage the SES identities mail is sent from.`,
	}
	cmd.AddCommand(newEmailSendCmd(opts))
	cmd.AddCommand(newSenderDomainCmd(opts))
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

Either --body or --body-html (or both) must be supplied. --body-file
and --body-html-file read content from disk.

If --from is omitted, the API picks the first verified sender domain
on your organization, or auto-mints a hail-mail address if one is
configured. See docs/setup/aws-ses.md.`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runEmailSend(cmd.Context(), opts, f)
		},
	}

	cmd.Flags().StringSliceVar(&f.to, "to", nil, "Recipient(s); repeat or comma-separate")
	cmd.Flags().StringSliceVar(&f.cc, "cc", nil, "CC recipient(s)")
	cmd.Flags().StringSliceVar(&f.bcc, "bcc", nil, "BCC recipient(s)")
	cmd.Flags().StringVar(&f.from, "from", "", "Override the from-address (must be a verified sender)")
	cmd.Flags().StringVar(&f.replyTo, "reply-to", "", "Reply-To header")
	cmd.Flags().StringVar(&f.subject, "subject", "", "Subject line (required)")
	cmd.Flags().StringVar(&f.body, "body", "", "Plain-text body")
	cmd.Flags().StringVar(&f.bodyHTML, "body-html", "", "HTML body")
	cmd.Flags().StringVar(&f.bodyFile, "body-file", "", "Read plain-text body from this file (use '-' for stdin)")
	cmd.Flags().StringVar(&f.bodyHTMLFile, "body-html-file", "", "Read HTML body from this file (use '-' for stdin)")
	cmd.Flags().StringVar(&f.idempotencyKey, "idempotency-key", "", "Defaults to a fresh UUID")

	if err := cmd.MarkFlagRequired("subject"); err != nil {
		// MarkFlagRequired only fails when the named flag doesn't exist; we
		// just registered it above, so this can never trip outside of a bug.
		panic(err)
	}

	return cmd
}

func runEmailSend(ctx context.Context, opts *Options, f *emailSendFlags) error {
	to, err := normalizeRecipients(f.to)
	if err != nil {
		return err
	}
	if len(to) == 0 {
		return fmt.Errorf("--to is required (at least one recipient)")
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
		return fmt.Errorf("must provide --body, --body-html, --body-file, or --body-html-file")
	}

	cc, err := normalizeRecipients(f.cc)
	if err != nil {
		return fmt.Errorf("--cc: %w", err)
	}
	bcc, err := normalizeRecipients(f.bcc)
	if err != nil {
		return fmt.Errorf("--bcc: %w", err)
	}

	body := client.EmailCreate{
		To:       to,
		Subject:  f.subject,
		From:     strPtr(f.from),
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

	idem := f.idempotencyKey
	if idem == "" {
		idem = uuid.NewString()
	}

	apiClient, err := opts.newClient(idempotencyEditor(idem))
	if err != nil {
		return err
	}

	resp, err := apiClient.CreateEmailEmailsPostWithResponse(
		ctx, &client.CreateEmailEmailsPostParams{}, body,
	)
	if err != nil {
		return fmt.Errorf("email API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusCreated || resp.JSON201 == nil {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}

	return printEmail(opts, resp.JSON201)
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
