package cmd

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"text/tabwriter"

	"github.com/google/uuid"
	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/hail-hq/hail/cli/internal/client"
)

// newSenderDomainCmd builds the `email sender-domain` subtree.
//
// Self-hosters don't have the managed-cloud web console, so the CLI is
// the only way for them to register a hail-mail row or a custom domain.
// Subcommand verbs follow the API: register, list, get, verify, delete.
func newSenderDomainCmd(opts *Options) *cobra.Command {
	cmd := &cobra.Command{
		Use:     "sender-domain",
		Aliases: []string{"sd"},
		Short:   "Manage email sender-domain identities",
		Long: `hail email sender-domain — manage the identities email is sent from.

Two flavors of identity (POST body 'kind' field):

  hail_mail   <user>+<org>@<HAIL_MAIL_BASE_DOMAIN> (operator-managed parent).
              Lands verified immediately; SES is never called.
  custom      Tenant DNS. SES returns 3 DKIM CNAMEs you must publish,
              then call 'verify' to flip the row to verified.

Subcommands:
  register    Register a new sender domain (hail_mail or custom).
  list        List org-scoped sender domains.
  get         Fetch one sender domain by id.
  verify      Re-poll the email provider for a custom row's DKIM status.
  delete      Remove a sender domain (custom rows are deleted in SES too).`,
	}
	cmd.AddCommand(newSenderDomainRegisterCmd(opts))
	cmd.AddCommand(newSenderDomainListCmd(opts))
	cmd.AddCommand(newSenderDomainGetCmd(opts))
	cmd.AddCommand(newSenderDomainVerifyCmd(opts))
	cmd.AddCommand(newSenderDomainDeleteCmd(opts))
	return cmd
}

// --------------------------------------------------------------------------- //
// register
// --------------------------------------------------------------------------- //

type senderDomainRegisterFlags struct {
	kind       string
	domain     string
	userPrefix string
	orgPrefix  string
	idemKey    string
}

func newSenderDomainRegisterCmd(opts *Options) *cobra.Command {
	f := &senderDomainRegisterFlags{}
	cmd := &cobra.Command{
		Use:   "register",
		Short: "Register a new sender domain",
		Long: `hail email sender-domain register — register a hail_mail or custom domain.

Examples:
  # Hail-mail using server env defaults (HAIL_MAIL_DEFAULT_*_PREFIX):
  hail email sender-domain register --kind hail_mail

  # Hail-mail with explicit prefixes:
  hail email sender-domain register --kind hail_mail \
      --local-prefix-user alice --local-prefix-org acme

  # Custom domain (returns DKIM CNAMEs to publish):
  hail email sender-domain register --kind custom --domain acme.com`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runSenderDomainRegister(cmd.Context(), opts, f)
		},
	}
	cmd.Flags().StringVar(&f.kind, "kind", "", "Identity kind: hail_mail or custom (required)")
	cmd.Flags().StringVar(&f.domain, "domain", "", "DNS domain (required for kind=custom; must be omitted for kind=hail_mail)")
	cmd.Flags().StringVar(&f.userPrefix, "local-prefix-user", "", "User-side local part for kind=hail_mail (falls back to HAIL_MAIL_DEFAULT_USER_PREFIX)")
	cmd.Flags().StringVar(&f.orgPrefix, "local-prefix-org", "", "Org-side local part for kind=hail_mail (falls back to HAIL_MAIL_DEFAULT_ORG_PREFIX)")
	cmd.Flags().StringVar(&f.idemKey, "idempotency-key", "", "Defaults to a fresh UUID")
	if err := cmd.MarkFlagRequired("kind"); err != nil {
		panic(err)
	}
	return cmd
}

func runSenderDomainRegister(
	ctx context.Context, opts *Options, f *senderDomainRegisterFlags,
) error {
	if f.kind != "hail_mail" && f.kind != "custom" {
		return errors.New("--kind must be 'hail_mail' or 'custom'")
	}

	body := client.SenderDomainCreate{
		Kind:            client.SenderDomainCreateKind(f.kind),
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

	resp, err := apiClient.CreateSenderDomainSenderDomainsPostWithResponse(
		ctx, &client.CreateSenderDomainSenderDomainsPostParams{}, body,
	)
	if err != nil {
		return fmt.Errorf("sender-domain API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusCreated || resp.JSON201 == nil {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}

	return printSenderDomain(opts, resp.JSON201)
}

// --------------------------------------------------------------------------- //
// list
// --------------------------------------------------------------------------- //

type senderDomainListFlags struct {
	limit  int
	cursor string
}

func newSenderDomainListCmd(opts *Options) *cobra.Command {
	f := &senderDomainListFlags{}
	cmd := &cobra.Command{
		Use:     "list",
		Aliases: []string{"ls"},
		Short:   "List sender domains for the calling org",
		Args:    cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runSenderDomainList(cmd.Context(), opts, f)
		},
	}
	cmd.Flags().IntVar(&f.limit, "limit", 50, "Page size (1..200)")
	cmd.Flags().StringVar(&f.cursor, "cursor", "", "Resume from a previous next_cursor")
	return cmd
}

func runSenderDomainList(
	ctx context.Context, opts *Options, f *senderDomainListFlags,
) error {
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}
	params := &client.ListSenderDomainsSenderDomainsGetParams{
		Limit:  &f.limit,
		Cursor: strPtr(f.cursor),
	}
	resp, err := apiClient.ListSenderDomainsSenderDomainsGetWithResponse(ctx, params)
	if err != nil {
		return fmt.Errorf("sender-domain API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}
	return printSenderDomainList(opts, resp.JSON200)
}

// --------------------------------------------------------------------------- //
// get
// --------------------------------------------------------------------------- //

func newSenderDomainGetCmd(opts *Options) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "get <id>",
		Short: "Fetch one sender domain by id",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			id, err := uuid.Parse(args[0])
			if err != nil {
				return fmt.Errorf("invalid id: %w", err)
			}
			return runSenderDomainGet(cmd.Context(), opts, id)
		},
	}
	return cmd
}

func runSenderDomainGet(ctx context.Context, opts *Options, id uuid.UUID) error {
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}
	resp, err := apiClient.GetSenderDomainSenderDomainsDomainIdGetWithResponse(
		ctx,
		openapi_types.UUID(id),
		&client.GetSenderDomainSenderDomainsDomainIdGetParams{},
	)
	if err != nil {
		return fmt.Errorf("sender-domain API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}
	return printSenderDomain(opts, resp.JSON200)
}

// --------------------------------------------------------------------------- //
// verify
// --------------------------------------------------------------------------- //

func newSenderDomainVerifyCmd(opts *Options) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "verify <id>",
		Short: "Re-poll the email provider for a custom row's verification status",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			id, err := uuid.Parse(args[0])
			if err != nil {
				return fmt.Errorf("invalid id: %w", err)
			}
			return runSenderDomainVerify(cmd.Context(), opts, id)
		},
	}
	return cmd
}

func runSenderDomainVerify(ctx context.Context, opts *Options, id uuid.UUID) error {
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}
	resp, err := apiClient.VerifySenderDomainSenderDomainsDomainIdVerifyPostWithResponse(
		ctx,
		openapi_types.UUID(id),
		&client.VerifySenderDomainSenderDomainsDomainIdVerifyPostParams{},
	)
	if err != nil {
		return fmt.Errorf("sender-domain API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}
	return printSenderDomain(opts, resp.JSON200)
}

// --------------------------------------------------------------------------- //
// delete
// --------------------------------------------------------------------------- //

func newSenderDomainDeleteCmd(opts *Options) *cobra.Command {
	cmd := &cobra.Command{
		Use:     "delete <id>",
		Aliases: []string{"rm"},
		Short:   "Delete a sender domain (and the SES identity for custom rows)",
		Args:    cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			id, err := uuid.Parse(args[0])
			if err != nil {
				return fmt.Errorf("invalid id: %w", err)
			}
			return runSenderDomainDelete(cmd.Context(), opts, id)
		},
	}
	return cmd
}

func runSenderDomainDelete(ctx context.Context, opts *Options, id uuid.UUID) error {
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}
	resp, err := apiClient.DeleteSenderDomainSenderDomainsDomainIdDelete(
		ctx,
		openapi_types.UUID(id),
		&client.DeleteSenderDomainSenderDomainsDomainIdDeleteParams{},
	)
	if err != nil {
		return fmt.Errorf("sender-domain API: %w", err)
	}
	if resp.StatusCode != http.StatusNoContent {
		// Bodyless DELETE — drain into a buffer for the error message.
		buf := make([]byte, 1024)
		n, _ := resp.Body.Read(buf)
		_ = resp.Body.Close()
		return apiError(resp.StatusCode, buf[:n])
	}
	_ = resp.Body.Close()
	fmt.Fprintf(opts.Stdout, "✓ Sender domain %s deleted\n", id)
	return nil
}

// --------------------------------------------------------------------------- //
// printers
// --------------------------------------------------------------------------- //

func printSenderDomain(opts *Options, sd *client.SenderDomainResponse) error {
	if opts.JSON {
		return printJSON(opts.Stdout, sd)
	}
	fmt.Fprintf(opts.Stdout, "✓ Sender domain %s: %s\n", string(sd.Kind), sd.Id.String())
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

func printSenderDomainList(opts *Options, body *client.SenderDomainListResponse) error {
	if opts.JSON {
		return printJSON(opts.Stdout, body)
	}
	if len(body.Items) == 0 {
		fmt.Fprintln(opts.Stdout, "(no sender domains)")
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

// _ keeps strings linkable even if all string-using helpers later move out.
var _ = strings.TrimSpace
