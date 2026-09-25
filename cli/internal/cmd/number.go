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
	provider   string
	quoteID    string
	sms        bool
	voice      bool
	idemKey    string
}

func newNumberAcquireCmd(opts *Options) *cobra.Command {
	f := &numberAcquireFlags{}
	cmd := &cobra.Command{
		Use:   "acquire",
		Short: "Acquire a new dedicated phone number",
		Long: `hail numbers acquire — buy a dedicated number at the cheapest live offer.

It requests live quotes (POST /numbers/quotes), picks the cheapest offer that
is ready to buy (monthly rental plus setup), and buys it with its quote id.
Pass --quote-id to buy a specific offer instead.

Examples:
  # A US local number (voice + SMS capable):
  hail numbers acquire --country US

  # A US toll-free number from Telnyx:
  hail numbers acquire --country US --type toll_free --provider telnyx

  # Buy one exact offer from an earlier quote:
  hail numbers acquire --country US --quote-id 8b1f0c2e-...`,
		Args:    cobra.NoArgs,
		PreRunE: requireMarkedFlags,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runNumberAcquire(cmd.Context(), cmd, opts, f)
		},
	}
	cmd.Flags().StringVar(&f.country, "country", "", "ISO 3166-1 alpha-2 country code (e.g. US)")
	cmd.Flags().StringVar(&f.numberType, "type", "local", "Number type: local, mobile, toll_free, or national")
	cmd.Flags().StringVar(&f.provider, "provider", "auto", "Carrier: auto, twilio, or telnyx")
	cmd.Flags().StringVar(&f.quoteID, "quote-id", "", "Buy this quote (from POST /numbers/quotes) instead of the cheapest offer")
	cmd.Flags().BoolVar(&f.voice, "voice-only", false, "Require voice only (default: voice and SMS)")
	cmd.Flags().BoolVar(&f.sms, "sms-only", false, "Require SMS only (default: voice and SMS)")
	cmd.Flags().StringVar(&f.idemKey, "idempotency-key", "", "Defaults to a fresh UUID")
	cmd.MarkFlagRequired("country")
	return cmd
}

func runNumberAcquire(ctx context.Context, cmd *cobra.Command, opts *Options, f *numberAcquireFlags) error {
	if f.country == "" {
		return requireInputs(cmd, "--country")
	}
	switch f.numberType {
	case "local", "mobile", "toll_free", "national":
	default:
		return helpAndFail(cmd, "--type must be 'local', 'mobile', 'toll_free', or 'national'")
	}
	switch f.provider {
	case "auto", "twilio", "telnyx":
	default:
		return helpAndFail(cmd, "--provider must be 'auto', 'twilio', or 'telnyx'")
	}
	if f.voice && f.sms {
		return helpAndFail(cmd, "--voice-only and --sms-only cannot be combined")
	}

	var quoteID openapi_types.UUID
	if f.quoteID != "" {
		parsed, err := uuid.Parse(f.quoteID)
		if err != nil {
			return helpAndFail(cmd, "--quote-id must be a UUID")
		}
		quoteID = openapi_types.UUID(parsed)
	} else {
		id, err := cheapestQuoteID(ctx, opts, f)
		if err != nil {
			return err
		}
		quoteID = id
	}

	provider := client.NumberAcquireRequestProvider(f.provider)
	body := client.NumberAcquireRequest{
		CountryCode: f.country,
		Provider:    &provider,
		QuoteId:     quoteID,
	}
	// An explicit --type is checked against the quote; without --quote-id the
	// quote was already requested for this type.
	if f.quoteID != "" && cmd.Flags().Changed("type") {
		nt := client.NumberAcquireRequestNumberType(f.numberType)
		body.NumberType = &nt
	}

	apiClient, err := opts.newClientWithIdempotency(f.idemKey)
	if err != nil {
		return err
	}

	resp, err := apiClient.AcquireNumberV1NumbersPostWithResponse(
		ctx, &client.AcquireNumberV1NumbersPostParams{}, body,
	)
	if err != nil {
		return fmt.Errorf("numbers API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusCreated || resp.JSON201 == nil {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}

	return printPhoneNumber(opts, resp.JSON201, true)
}

// cheapestQuoteID requests live offers and returns the ready offer with the
// lowest monthly plus setup price. The first such offer wins a tie, which is
// the API's own ranking.
func cheapestQuoteID(ctx context.Context, opts *Options, f *numberAcquireFlags) (openapi_types.UUID, error) {
	caps := []client.NumberQuoteRequestCapabilities{
		client.NumberQuoteRequestCapabilities("voice"),
		client.NumberQuoteRequestCapabilities("sms"),
	}
	if f.voice {
		caps = caps[:1]
	}
	if f.sms {
		caps = caps[1:]
	}
	nt := client.NumberQuoteRequestNumberType(f.numberType)
	provider := client.NumberQuoteRequestProvider(f.provider)
	apiClient, err := opts.newClient()
	if err != nil {
		return openapi_types.UUID{}, err
	}
	resp, err := apiClient.QuoteNumbersV1NumbersQuotesPostWithResponse(
		ctx, &client.QuoteNumbersV1NumbersQuotesPostParams{},
		client.NumberQuoteRequest{
			CountryCode:  f.country,
			NumberType:   &nt,
			Capabilities: caps,
			Provider:     &provider,
		},
	)
	if err != nil {
		return openapi_types.UUID{}, fmt.Errorf("numbers API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
		return openapi_types.UUID{}, apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}
	var best *client.CarrierOffer
	for i := range resp.JSON200.Offers {
		o := &resp.JSON200.Offers[i]
		if o.QuoteId == nil || o.Readiness != client.CarrierOfferReadiness("ready") {
			continue
		}
		if best == nil || o.MonthlyCents+o.SetupCents < best.MonthlyCents+best.SetupCents {
			best = o
		}
	}
	if best == nil {
		return openapi_types.UUID{}, fmt.Errorf("no number ready to buy in %s (%s); complete regulatory verification or try another --type or --provider", f.country, f.numberType)
	}
	return *best.QuoteId, nil
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
	params := &client.ListNumbersV1NumbersGetParams{
		Limit:  &f.limit,
		Cursor: strPtr(f.cursor),
	}
	resp, err := apiClient.ListNumbersV1NumbersGetWithResponse(ctx, params)
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
	resp, err := apiClient.GetNumberV1NumbersNumberIdGetWithResponse(
		ctx,
		openapi_types.UUID(id),
		&client.GetNumberV1NumbersNumberIdGetParams{},
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
	resp, err := apiClient.EnableSmsV1NumbersNumberIdEnableSmsPostWithResponse(
		ctx,
		openapi_types.UUID(id),
		&client.EnableSmsV1NumbersNumberIdEnableSmsPostParams{},
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
