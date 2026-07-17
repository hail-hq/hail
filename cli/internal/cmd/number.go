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

// newNumberCmd builds the `numbers` subtree.
//
// A dedicated PhoneNumber is a cross-channel resource (voice + SMS), not
// SMS-specific — acquisition and listing live under a top-level `numbers`
// command rather than under `sms`. Subcommand verbs follow the API:
// acquire, list, get, enable-sms.
func newNumberCmd(opts *Options) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "numbers",
		Short: "Manage dedicated phone numbers (voice + SMS)",
		Long: `hail numbers — acquire and manage dedicated phone numbers.

A dedicated number is a cross-channel resource: the same number can carry
voice calls and (once SMS is enabled) text messages. Capabilities are fixed
by the carrier at purchase time — acquire a new number if you need one the
existing number doesn't support.`,
	}
	cmd.AddCommand(newNumberAcquireCmd(opts))
	cmd.AddCommand(newNumberListCmd(opts))
	cmd.AddCommand(newNumberGetCmd(opts))
	cmd.AddCommand(newNumberEnableSmsCmd(opts))
	return cmd
}

// --------------------------------------------------------------------------- //
// acquire
// --------------------------------------------------------------------------- //

type numberAcquireFlags struct {
	country    string
	numberType string
	idemKey    string
}

func newNumberAcquireCmd(opts *Options) *cobra.Command {
	f := &numberAcquireFlags{}
	cmd := &cobra.Command{
		Use:   "acquire",
		Short: "Acquire a new dedicated phone number",
		Long: `hail numbers acquire — provision a new dedicated number from the carrier.

Examples:
  # A US local number (voice + SMS capable):
  hail numbers acquire --country US

  # A US toll-free number:
  hail numbers acquire --country US --type toll_free`,
		Args:    cobra.NoArgs,
		PreRunE: requireMarkedFlags,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runNumberAcquire(cmd.Context(), cmd, opts, f)
		},
	}
	cmd.Flags().StringVar(&f.country, "country", "", "ISO 3166-1 alpha-2 country code (e.g. US)")
	cmd.Flags().StringVar(&f.numberType, "type", "local", "Number type: local, mobile, or toll_free")
	cmd.Flags().StringVar(&f.idemKey, "idempotency-key", "", "Defaults to a fresh UUID")
	cmd.MarkFlagRequired("country")
	return cmd
}

func runNumberAcquire(ctx context.Context, cmd *cobra.Command, opts *Options, f *numberAcquireFlags) error {
	if f.country == "" {
		return requireInputs(cmd, "--country")
	}
	switch f.numberType {
	case "local", "mobile", "toll_free":
	default:
		return helpAndFail(cmd, "--type must be 'local', 'mobile', or 'toll_free'")
	}

	nt := client.NumberAcquireRequestNumberType(f.numberType)
	body := client.NumberAcquireRequest{
		CountryCode: f.country,
		NumberType:  &nt,
	}

	apiClient, err := opts.newClientWithIdempotency(f.idemKey)
	if err != nil {
		return err
	}

	resp, err := apiClient.AcquireNumberNumbersPostWithResponse(
		ctx, &client.AcquireNumberNumbersPostParams{}, body,
	)
	if err != nil {
		return fmt.Errorf("numbers API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusCreated || resp.JSON201 == nil {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}

	return printPhoneNumber(opts, resp.JSON201, true)
}

// --------------------------------------------------------------------------- //
// list
// --------------------------------------------------------------------------- //

type numberListFlags struct {
	limit  int
	cursor string
}

func newNumberListCmd(opts *Options) *cobra.Command {
	f := &numberListFlags{}
	cmd := &cobra.Command{
		Use:     "list",
		Aliases: []string{"ls"},
		Short:   "List dedicated numbers for the calling org",
		Args:    cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runNumberList(cmd.Context(), opts, f)
		},
	}
	cmd.Flags().IntVar(&f.limit, "limit", 50, "Page size (1..200)")
	cmd.Flags().StringVar(&f.cursor, "cursor", "", "Resume from a previous next_cursor")
	return cmd
}

func runNumberList(ctx context.Context, opts *Options, f *numberListFlags) error {
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}
	params := &client.ListNumbersNumbersGetParams{
		Limit:  &f.limit,
		Cursor: strPtr(f.cursor),
	}
	resp, err := apiClient.ListNumbersNumbersGetWithResponse(ctx, params)
	if err != nil {
		return fmt.Errorf("numbers API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}
	return printPhoneNumberList(opts, resp.JSON200)
}

// --------------------------------------------------------------------------- //
// get
// --------------------------------------------------------------------------- //

func newNumberGetCmd(opts *Options) *cobra.Command {
	return &cobra.Command{
		Use:   "get <id>",
		Short: "Fetch one dedicated number by id (full UUID or 4+ char prefix)",
		Args:  argsOrHelp(1, "<id>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			id, err := resolveNumberID(cmd.Context(), apiClient, args[0])
			if err != nil {
				return err
			}
			return runNumberGet(cmd.Context(), opts, id)
		},
	}
}

func runNumberGet(ctx context.Context, opts *Options, id uuid.UUID) error {
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}
	resp, err := apiClient.GetNumberNumbersNumberIdGetWithResponse(
		ctx,
		openapi_types.UUID(id),
		&client.GetNumberNumbersNumberIdGetParams{},
	)
	if err != nil {
		return fmt.Errorf("numbers API: %w", err)
	}
	if resp.HTTPResponse.StatusCode == http.StatusNotFound {
		return fmt.Errorf("number %s not found (or not in your org)", id.String())
	}
	if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}
	return printPhoneNumber(opts, resp.JSON200, false)
}

// --------------------------------------------------------------------------- //
// enable-sms
// --------------------------------------------------------------------------- //

func newNumberEnableSmsCmd(opts *Options) *cobra.Command {
	return &cobra.Command{
		Use:   "enable-sms <id>",
		Short: "Attach a Messaging Service so the number can send SMS (full UUID or 4+ char prefix)",
		Args:  argsOrHelp(1, "<id>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			id, err := resolveNumberID(cmd.Context(), apiClient, args[0])
			if err != nil {
				return err
			}
			return runNumberEnableSms(cmd.Context(), opts, id)
		},
	}
}

func runNumberEnableSms(ctx context.Context, opts *Options, id uuid.UUID) error {
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}
	resp, err := apiClient.EnableSmsNumbersNumberIdEnableSmsPostWithResponse(
		ctx,
		openapi_types.UUID(id),
		&client.EnableSmsNumbersNumberIdEnableSmsPostParams{},
	)
	if err != nil {
		return fmt.Errorf("numbers API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}
	return printPhoneNumber(opts, resp.JSON200, true)
}

// --------------------------------------------------------------------------- //
// printers
// --------------------------------------------------------------------------- //

func printPhoneNumber(opts *Options, n *client.PhoneNumberResponse, banner bool) error {
	if opts.JSON {
		return printJSON(opts.Stdout, n)
	}
	if banner {
		fmt.Fprintf(opts.Stdout, "✓ Number %s: %s\n", n.E164, n.Id.String())
	} else {
		fmt.Fprintf(opts.Stdout, "Number %s: %s\n", n.E164, n.Id.String())
	}
	fmt.Fprintf(opts.Stdout, "  Country:      %s\n", n.CountryCode)
	fmt.Fprintf(opts.Stdout, "  Type:         %s\n", n.NumberType)
	fmt.Fprintf(opts.Stdout, "  Capabilities: %s\n", strings.Join(n.Capabilities, ", "))
	fmt.Fprintf(opts.Stdout, "  State:        %s\n", n.ProvisioningState)
	if n.MessagingServiceSid != nil && *n.MessagingServiceSid != "" {
		fmt.Fprintf(opts.Stdout, "  Messaging:    %s\n", *n.MessagingServiceSid)
	}
	return nil
}

func printPhoneNumberList(opts *Options, body *client.PhoneNumberListResponse) error {
	if opts.JSON {
		return printJSON(opts.Stdout, body)
	}
	if len(body.Items) == 0 {
		fmt.Fprintln(opts.Stdout, "(no numbers)")
		return nil
	}
	w := tabwriter.NewWriter(opts.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(w, "ID\tE164\tTYPE\tCAPABILITIES\tSTATE")
	for _, n := range body.Items {
		fmt.Fprintf(
			w, "%s\t%s\t%s\t%s\t%s\n",
			n.Id.String(),
			n.E164,
			n.NumberType,
			strings.Join(n.Capabilities, ","),
			n.ProvisioningState,
		)
	}
	_ = w.Flush()
	if body.NextCursor != nil && *body.NextCursor != "" {
		fmt.Fprintf(opts.Stdout, "\nMore: --cursor %s\n", *body.NextCursor)
	}
	return nil
}
