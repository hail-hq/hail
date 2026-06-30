package cmd

import (
	"context"
	"fmt"
	"net/http"
	"text/tabwriter"

	"github.com/google/uuid"
	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/hail-hq/hail/cli/internal/client"
)

// newEmailDomainCmd builds the `email domain` subtree.
//
// Self-hosters don't have the managed-cloud web console, so the CLI is
// the only way for them to register a hail-mail row or a custom domain.
// Subcommand verbs follow the API: register, list, get, verify, delete.
func newEmailDomainCmd(opts *Options) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "domain",
		Short: "Manage email domain identities (send + receive)",
		Long: `hail email domain — manage the identities email is sent from and received on.

Two flavors of identity (POST body 'kind' field):

  hail_mail   <user>+<org>@<HAIL_MAIL_BASE_DOMAIN> (operator-managed parent).
              Lands verified immediately; SES is never called.
  custom      Tenant DNS. SES returns 3 DKIM CNAMEs you must publish,
              then call 'verify' to flip the row to verified.`,
	}
	cmd.AddCommand(newEmailDomainRegisterCmd(opts))
	cmd.AddCommand(newEmailDomainListCmd(opts))
	cmd.AddCommand(newEmailDomainGetCmd(opts))
	cmd.AddCommand(newEmailDomainVerifyCmd(opts))
	cmd.AddCommand(newEmailDomainDeleteCmd(opts))
	return cmd
}

// --------------------------------------------------------------------------- //
// register
// --------------------------------------------------------------------------- //

type emailDomainRegisterFlags struct {
	kind       string
	domain     string
	userPrefix string
	orgPrefix  string
	idemKey    string
}

func newEmailDomainRegisterCmd(opts *Options) *cobra.Command {
	f := &emailDomainRegisterFlags{}
	cmd := &cobra.Command{
		Use:   "register",
		Short: "Register a new email domain",
		Long: `hail email domain register — register a hail_mail or custom domain.

Examples:
  # Hail-mail using server env defaults (HAIL_MAIL_DEFAULT_*_PREFIX):
  hail email domain register --kind hail_mail

  # Hail-mail with explicit prefixes:
  hail email domain register --kind hail_mail \
      --local-prefix-user alice --local-prefix-org acme

  # Custom domain (returns DKIM CNAMEs to publish):
  hail email domain register --kind custom --domain acme.com`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runEmailDomainRegister(cmd.Context(), cmd, opts, f)
		},
	}
	cmd.Flags().StringVar(&f.kind, "kind", "", "Identity kind: hail_mail or custom (required)")
	cmd.Flags().StringVar(&f.domain, "domain", "", "DNS domain (required for kind=custom; must be omitted for kind=hail_mail)")
	cmd.Flags().StringVar(&f.userPrefix, "local-prefix-user", "", "User-side local part for kind=hail_mail (falls back to HAIL_MAIL_DEFAULT_USER_PREFIX)")
	cmd.Flags().StringVar(&f.orgPrefix, "local-prefix-org", "", "Org-side local part for kind=hail_mail (falls back to HAIL_MAIL_DEFAULT_ORG_PREFIX)")
	cmd.Flags().StringVar(&f.idemKey, "idempotency-key", "", "Defaults to a fresh UUID")
	return cmd
}

func runEmailDomainRegister(
	ctx context.Context, cmd *cobra.Command, opts *Options, f *emailDomainRegisterFlags,
) error {
	if f.kind == "" {
		return requireInputs(cmd, "--kind")
	}
	if f.kind != "hail_mail" && f.kind != "custom" {
		return helpAndFail(cmd, "--kind must be 'hail_mail' or 'custom'")
	}

	body := client.EmailDomainCreate{
		Kind:            client.EmailDomainCreateKind(f.kind),
		Domain:          strPtr(f.domain),
		LocalPrefixUser: strPtr(f.userPrefix),
		LocalPrefixOrg:  strPtr(f.orgPrefix),
	}

	idem := f.idemKey
	if idem == "" {
		idem = uuid.NewString()
	}

	apiClient, err := opts.newClient(idempotencyEditor(idem))
	if err != nil {
		return err
	}

	resp, err := apiClient.CreateEmailDomainEmailDomainsPostWithResponse(
		ctx, &client.CreateEmailDomainEmailDomainsPostParams{}, body,
	)
	if err != nil {
		return fmt.Errorf("email-domain API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusCreated || resp.JSON201 == nil {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}

	return printEmailDomain(opts, resp.JSON201)
}

// --------------------------------------------------------------------------- //
// list
// --------------------------------------------------------------------------- //

type emailDomainListFlags struct {
	limit  int
	cursor string
}

func newEmailDomainListCmd(opts *Options) *cobra.Command {
	f := &emailDomainListFlags{}
	cmd := &cobra.Command{
		Use:     "list",
		Aliases: []string{"ls"},
		Short:   "List email domains for the calling org",
		Args:    cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runEmailDomainList(cmd.Context(), opts, f)
		},
	}
	cmd.Flags().IntVar(&f.limit, "limit", 50, "Page size (1..200)")
	cmd.Flags().StringVar(&f.cursor, "cursor", "", "Resume from a previous next_cursor")
	return cmd
}

func runEmailDomainList(
	ctx context.Context, opts *Options, f *emailDomainListFlags,
) error {
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}
	params := &client.ListEmailDomainsEmailDomainsGetParams{
		Limit:  &f.limit,
		Cursor: strPtr(f.cursor),
	}
	resp, err := apiClient.ListEmailDomainsEmailDomainsGetWithResponse(ctx, params)
	if err != nil {
		return fmt.Errorf("email-domain API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}
	return printEmailDomainList(opts, resp.JSON200)
}

// --------------------------------------------------------------------------- //
// get
// --------------------------------------------------------------------------- //

func newEmailDomainGetCmd(opts *Options) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "get <id>",
		Short: "Fetch one email domain by id (full UUID or 4+ char prefix)",
		Args:  argsOrHelp(1, "<id>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			id, err := resolveEmailDomainID(cmd.Context(), apiClient, args[0])
			if err != nil {
				return err
			}
			return runEmailDomainGet(cmd.Context(), opts, id)
		},
	}
	return cmd
}

func runEmailDomainGet(ctx context.Context, opts *Options, id uuid.UUID) error {
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}
	resp, err := apiClient.GetEmailDomainEmailDomainsDomainIdGetWithResponse(
		ctx,
		openapi_types.UUID(id),
		&client.GetEmailDomainEmailDomainsDomainIdGetParams{},
	)
	if err != nil {
		return fmt.Errorf("email-domain API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}
	return printEmailDomain(opts, resp.JSON200)
}

// --------------------------------------------------------------------------- //
// verify
// --------------------------------------------------------------------------- //

func newEmailDomainVerifyCmd(opts *Options) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "verify <id>",
		Short: "Re-poll the email provider for a custom row's verification status (full UUID or 4+ char prefix)",
		Args:  argsOrHelp(1, "<id>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			id, err := resolveEmailDomainID(cmd.Context(), apiClient, args[0])
			if err != nil {
				return err
			}
			return runEmailDomainVerify(cmd.Context(), opts, id)
		},
	}
	return cmd
}

func runEmailDomainVerify(ctx context.Context, opts *Options, id uuid.UUID) error {
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}
	resp, err := apiClient.VerifyEmailDomainEmailDomainsDomainIdVerifyPostWithResponse(
		ctx,
		openapi_types.UUID(id),
		&client.VerifyEmailDomainEmailDomainsDomainIdVerifyPostParams{},
	)
	if err != nil {
		return fmt.Errorf("email-domain API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}
	return printEmailDomain(opts, resp.JSON200)
}

// --------------------------------------------------------------------------- //
// delete
// --------------------------------------------------------------------------- //

func newEmailDomainDeleteCmd(opts *Options) *cobra.Command {
	cmd := &cobra.Command{
		Use:     "delete <id>",
		Aliases: []string{"rm"},
		Short:   "Delete an email domain (full UUID or 4+ char prefix). Also drops the SES identity for custom rows.",
		Args:    argsOrHelp(1, "<id>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			id, err := resolveEmailDomainID(cmd.Context(), apiClient, args[0])
			if err != nil {
				return err
			}
			return runEmailDomainDelete(cmd.Context(), opts, id)
		},
	}
	return cmd
}

func runEmailDomainDelete(ctx context.Context, opts *Options, id uuid.UUID) error {
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}
	resp, err := apiClient.DeleteEmailDomainEmailDomainsDomainIdDelete(
		ctx,
		openapi_types.UUID(id),
		&client.DeleteEmailDomainEmailDomainsDomainIdDeleteParams{},
	)
	if err != nil {
		return fmt.Errorf("email-domain API: %w", err)
	}
	if resp.StatusCode != http.StatusNoContent {
		// Bodyless DELETE — drain into a buffer for the error message.
		buf := make([]byte, 1024)
		n, _ := resp.Body.Read(buf)
		_ = resp.Body.Close()
		return apiError(resp.StatusCode, buf[:n])
	}
	_ = resp.Body.Close()
	fmt.Fprintf(opts.Stdout, "✓ Email domain %s deleted\n", id)
	return nil
}

// --------------------------------------------------------------------------- //
// printers
// --------------------------------------------------------------------------- //

func printEmailDomain(opts *Options, sd *client.EmailDomainResponse) error {
	if opts.JSON {
		return printJSON(opts.Stdout, sd)
	}
	fmt.Fprintf(opts.Stdout, "✓ Email domain %s: %s\n", string(sd.Kind), sd.Id.String())
	fmt.Fprintf(opts.Stdout, "  Domain:       %s\n", sd.Domain)
	if sd.LocalPrefixUser != nil && *sd.LocalPrefixUser != "" {
		fmt.Fprintf(opts.Stdout, "  User prefix:  %s\n", *sd.LocalPrefixUser)
	}
	if sd.LocalPrefixOrg != nil && *sd.LocalPrefixOrg != "" {
		fmt.Fprintf(opts.Stdout, "  Org prefix:   %s\n", *sd.LocalPrefixOrg)
	}
	fmt.Fprintf(opts.Stdout, "  Verification: %s\n", string(sd.VerificationStatus))
	if len(sd.DkimRecords) > 0 {
		fmt.Fprintln(opts.Stdout, "  Publish these CNAMEs at your DNS provider:")
		w := tabwriter.NewWriter(opts.Stdout, 0, 0, 2, ' ', 0)
		fmt.Fprintln(w, "    TYPE\tNAME\tVALUE")
		for _, r := range sd.DkimRecords {
			typ := "CNAME"
			if r.Type != nil {
				typ = *r.Type
			}
			fmt.Fprintf(w, "    %s\t%s\t%s\n", typ, r.Name, r.Value)
		}
		_ = w.Flush()
	}
	return nil
}

func printEmailDomainList(opts *Options, body *client.EmailDomainListResponse) error {
	if opts.JSON {
		return printJSON(opts.Stdout, body)
	}
	if len(body.Items) == 0 {
		fmt.Fprintln(opts.Stdout, "(no email domains)")
		return nil
	}
	w := tabwriter.NewWriter(opts.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(w, "ID\tKIND\tDOMAIN\tSTATUS")
	for _, sd := range body.Items {
		fmt.Fprintf(
			w, "%s\t%s\t%s\t%s\n",
			sd.Id.String(),
			string(sd.Kind),
			sd.Domain,
			string(sd.VerificationStatus),
		)
	}
	_ = w.Flush()
	if body.NextCursor != nil && *body.NextCursor != "" {
		fmt.Fprintf(opts.Stdout, "\nMore: --cursor %s\n", *body.NextCursor)
	}
	return nil
}
