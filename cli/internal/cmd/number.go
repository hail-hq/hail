package cmd

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
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
// acquire, quotes, list, get, enable-sms.
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
	cmd.AddCommand(newNumberQuotesCmd(opts))
	cmd.AddCommand(newNumberListCmd(opts))
	cmd.AddCommand(newNumberGetCmd(opts))
	cmd.AddCommand(newNumberEnableSmsCmd(opts))
	cmd.AddCommand(newNumberRouteCmd(opts))
	cmd.AddCommand(newNumberRegisterSmsCmd(opts))
	cmd.AddCommand(newNumberConfirmSmsCmd(opts))
	cmd.AddCommand(newNumberCancelSmsCmd(opts))
	return cmd
}

// --------------------------------------------------------------------------- //
// acquire
// --------------------------------------------------------------------------- //

type numberAcquireFlags struct {
	country    string
	numberType string
	areaCode   string
	city       string
	region     string
	quoteID    string
	sms        bool
	voice      bool
	idemKey    string
	expected   int
	subject    string
	verifyID   string
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

  # A local number in the 480 area code (Phoenix, Arizona):
  hail numbers acquire --country US --area-code 480

  # A local number in London (area code as dialed in the UK):
  hail numbers acquire --country GB --area-code 020

  # A local number in Phoenix, Arizona (--region is optional):
  hail numbers acquire --country US --city Phoenix --region AZ

  # A US toll-free number:
  hail numbers acquire --country US --type toll_free

  # Buy one exact offer from an earlier quote:
  hail numbers acquire --country US --quote-id 8b1f0c2e-...

  # More than one approved verification for this number? List them with
  # "hail numbers quotes --country GB --type mobile", then pick one by its id:
  hail numbers acquire --country GB --type mobile --quote-id 8b1f0c2e-... \
    --verification-id 3c9a7d10-...`,
		Args:    cobra.NoArgs,
		PreRunE: requireMarkedFlags,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runNumberAcquire(cmd.Context(), cmd, opts, f)
		},
	}
	addQuoteFlags(cmd, f)
	cmd.Flags().StringVar(&f.quoteID, "quote-id", "", "Buy this quote (from POST /numbers/quotes) instead of the cheapest offer")
	cmd.Flags().StringVar(&f.idemKey, "idempotency-key", "", "Defaults to a fresh UUID")
	cmd.Flags().IntVar(&f.expected, "expected-total-cents", 0, "Refuse the order (409) if the quoted monthly plus setup price differs from this many cents")
	cmd.Flags().StringVar(&f.subject, "subject-type", "", "Buy only with an approved verification of this kind: person or business")
	cmd.Flags().StringVar(&f.verifyID, "verification-id", "", "Which approved verification buys the number (an id from hail numbers quotes). Needed only when more than one is approved")
	return cmd
}

func runNumberAcquire(ctx context.Context, cmd *cobra.Command, opts *Options, f *numberAcquireFlags) error {
	if err := checkQuoteFlags(cmd, f); err != nil {
		return err
	}
	if f.subject != "" && f.subject != "person" && f.subject != "business" {
		return helpAndFail(cmd, "--subject-type must be 'person' or 'business'")
	}
	var verificationID *openapi_types.UUID
	if f.verifyID != "" {
		parsed, err := uuid.Parse(f.verifyID)
		if err != nil {
			return helpAndFail(cmd, "--verification-id must be a UUID")
		}
		id := openapi_types.UUID(parsed)
		verificationID = &id
	}

	var quoteID openapi_types.UUID
	if f.quoteID != "" {
		parsed, err := uuid.Parse(f.quoteID)
		if err != nil {
			return helpAndFail(cmd, "--quote-id must be a UUID")
		}
		quoteID = openapi_types.UUID(parsed)
	} else {
		id, err := cheapestQuoteID(ctx, opts, f, verificationID)
		if err != nil {
			return err
		}
		quoteID = id
	}

	body := client.NumberAcquireRequest{
		CountryCode: f.country,
		QuoteId:     quoteID,
	}
	if cmd.Flags().Changed("expected-total-cents") {
		if f.expected < 0 {
			return helpAndFail(cmd, "--expected-total-cents must be 0 or more")
		}
		body.ExpectedTotalCents = &f.expected
	}
	if f.subject != "" {
		st := client.NumberAcquireRequestSubjectType(f.subject)
		body.SubjectType = &st
	}
	body.VerificationId = verificationID
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
		err := apiError(resp.HTTPResponse.StatusCode, resp.Body)
		if resp.HTTPResponse.StatusCode == http.StatusUnprocessableEntity &&
			bytes.Contains(resp.Body, []byte("verification_id")) {
			// Without --type the API took the type from the quote: unknown here.
			return fmt.Errorf("%w\nSee the verifications you can pass as --verification-id:\n  %s", err, quotesHint(f, f.quoteID == "" || cmd.Flags().Changed("type")))
		}
		return err
	}

	return printPhoneNumber(opts, resp.JSON201, true)
}

// quotesHint is the command that lists the offers and their verifications
// for the number the user asked for. typeKnown is false when --type was left
// to the quote: the hint then names the flag's values instead of a guess.
func quotesHint(f *numberAcquireFlags, typeKnown bool) string {
	numberType := "<local|mobile|toll_free|national>"
	if typeKnown {
		numberType = f.numberType
	}
	hint := "hail numbers quotes --country " + f.country + " --type " + numberType
	for _, flag := range []struct{ name, value string }{
		{"--area-code", f.areaCode}, {"--city", f.city}, {"--region", f.region},
	} {
		if flag.value != "" {
			hint += " " + flag.name + " " + shellQuote(flag.value)
		}
	}
	if f.voice {
		hint += " --voice-only"
	}
	if f.sms {
		hint += " --sms-only"
	}
	return hint
}

// shellQuote quotes a flag value that holds a space or a quote.
func shellQuote(v string) string {
	if strings.ContainsAny(v, " '\"") {
		return "'" + strings.ReplaceAll(v, "'", `'\''`) + "'"
	}
	return v
}

// requestQuotes asks POST /numbers/quotes for live offers matching the
// country, type, place and capability flags.
func requestQuotes(ctx context.Context, opts *Options, f *numberAcquireFlags) (*client.NumberQuotesResponse, error) {
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
	var areaCode, city, region *string
	if f.areaCode != "" {
		areaCode = &f.areaCode
	}
	if f.city != "" {
		city = &f.city
	}
	if f.region != "" {
		region = &f.region
	}
	apiClient, err := opts.newClient()
	if err != nil {
		return nil, err
	}
	resp, err := apiClient.QuoteNumbersV1NumbersQuotesPostWithResponse(
		ctx, &client.QuoteNumbersV1NumbersQuotesPostParams{},
		client.NumberQuoteRequest{
			CountryCode:  f.country,
			NumberType:   &nt,
			Capabilities: caps,
			AreaCode:     areaCode,
			City:         city,
			Region:       region,
		},
	)
	if err != nil {
		return nil, fmt.Errorf("numbers API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
		return nil, apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}
	return resp.JSON200, nil
}

// cheapestQuoteID requests live offers and returns the ready offer with the
// lowest monthly plus setup price. The first such offer wins a tie, which is
// the API's own ranking. With verificationID, only offers that list it as
// approved count: a verification belongs to one carrier's offers.
func cheapestQuoteID(ctx context.Context, opts *Options, f *numberAcquireFlags, verificationID *openapi_types.UUID) (openapi_types.UUID, error) {
	quotes, err := requestQuotes(ctx, opts, f)
	if err != nil {
		return openapi_types.UUID{}, err
	}
	var best *client.QuoteOffer
	allByRequest := len(quotes.Offers) > 0
	for i := range quotes.Offers {
		o := &quotes.Offers[i]
		if o.ByRequest == nil || !*o.ByRequest {
			allByRequest = false
		}
		// A by-request offer is never bought here: support orders it.
		if o.QuoteId == nil || o.Readiness != client.QuoteOfferReadiness("ready") || (o.ByRequest != nil && *o.ByRequest) {
			continue
		}
		if verificationID != nil && !listsApproved(o, *verificationID) {
			continue
		}
		if best == nil || o.MonthlyCents+o.SetupCents < best.MonthlyCents+best.SetupCents {
			best = o
		}
	}
	if best == nil && allByRequest {
		return openapi_types.UUID{}, fmt.Errorf("numbers in %s (%s) are set up by our team, not bought through the API; ask support for one", f.country, f.numberType)
	}
	if best == nil {
		return openapi_types.UUID{}, fmt.Errorf("no number ready to buy in %s (%s); complete regulatory verification or try another --type\nSee the offers and your verifications:\n  %s", f.country, f.numberType, quotesHint(f, true))
	}
	return *best.QuoteId, nil
}

// listsApproved reports whether the offer lists id as an approved verification.
func listsApproved(o *client.QuoteOffer, id openapi_types.UUID) bool {
	if o.Verifications == nil {
		return false
	}
	for _, v := range *o.Verifications {
		if v.Id == id && v.State == "approved" {
			return true
		}
	}
	return false
}

// --------------------------------------------------------------------------- //
// quotes
// --------------------------------------------------------------------------- //

func newNumberQuotesCmd(opts *Options) *cobra.Command {
	f := &numberAcquireFlags{}
	cmd := &cobra.Command{
		Use:   "quotes",
		Short: "List live offers and the verifications that can buy each one",
		Long: `hail numbers quotes: list live offers for a number, without buying.

It calls POST /numbers/quotes with the same flags as acquire. Each offer
shows its number, type, monthly and setup price, readiness and quote id.
Under it are your verifications for that offer: id, name, person or
company, and state. Pass an approved one's id to
"hail numbers acquire --quote-id <quote-id> --verification-id <id>".

Examples:
  hail numbers quotes --country US
  hail numbers quotes --country GB --type mobile
  hail numbers quotes --country GB --type mobile --json`,
		Args:    cobra.NoArgs,
		PreRunE: requireMarkedFlags,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runNumberQuotes(cmd.Context(), cmd, opts, f)
		},
	}
	addQuoteFlags(cmd, f)
	return cmd
}

// addQuoteFlags registers the flags POST /numbers/quotes takes, shared by
// acquire and quotes. --country is required.
func addQuoteFlags(cmd *cobra.Command, f *numberAcquireFlags) {
	cmd.Flags().StringVar(&f.country, "country", "", "ISO 3166-1 alpha-2 country code (e.g. US)")
	cmd.Flags().StringVar(&f.numberType, "type", "local", "Number type: local, mobile, toll_free, or national")
	cmd.Flags().StringVar(&f.areaCode, "area-code", "", "Area code or first digits of the number, as dialed in the country (e.g. 020, 480)")
	cmd.Flags().StringVar(&f.city, "city", "", "City name (e.g. London)")
	cmd.Flags().StringVar(&f.region, "region", "", "Optional, with --city: US state or CA province code (e.g. AZ)")
	cmd.Flags().BoolVar(&f.voice, "voice-only", false, "Require voice only (default: voice and SMS)")
	cmd.Flags().BoolVar(&f.sms, "sms-only", false, "Require SMS only (default: voice and SMS)")
	cmd.MarkFlagRequired("country")
}

// checkQuoteFlags validates the flags addQuoteFlags registers.
func checkQuoteFlags(cmd *cobra.Command, f *numberAcquireFlags) error {
	if f.country == "" {
		return requireInputs(cmd, "--country")
	}
	switch f.numberType {
	case "local", "mobile", "toll_free", "national":
	default:
		return helpAndFail(cmd, "--type must be 'local', 'mobile', 'toll_free', or 'national'")
	}
	if f.voice && f.sms {
		return helpAndFail(cmd, "--voice-only and --sms-only cannot be combined")
	}
	return nil
}

func runNumberQuotes(ctx context.Context, cmd *cobra.Command, opts *Options, f *numberAcquireFlags) error {
	if err := checkQuoteFlags(cmd, f); err != nil {
		return err
	}
	quotes, err := requestQuotes(ctx, opts, f)
	if err != nil {
		return err
	}
	return printQuotes(opts, quotes)
}

// printQuotes prints one line per offer and, indented under it, the
// verifications that can buy it.
func printQuotes(opts *Options, body *client.NumberQuotesResponse) error {
	if opts.JSON {
		return printJSON(opts.Stdout, body)
	}
	if len(body.Offers) == 0 {
		fmt.Fprintln(opts.Stdout, "(no offers)")
		return nil
	}
	for _, o := range body.Offers {
		number := "(by request)"
		if o.E164 != nil && *o.E164 != "" {
			number = *o.E164
		}
		quoteID := "-"
		if o.QuoteId != nil {
			quoteID = o.QuoteId.String()
		}
		fmt.Fprintf(
			opts.Stdout, "%s  %s  %s/mo + %s setup  %s  quote %s\n",
			number, o.NumberType, cents(o.MonthlyCents), cents(o.SetupCents),
			o.Readiness, quoteID,
		)
		if o.Verifications == nil {
			continue
		}
		for _, v := range *o.Verifications {
			name := "(no name)"
			if v.SubjectName != nil && *v.SubjectName != "" {
				name = *v.SubjectName
			}
			who := "person"
			if v.SubjectType == "business" {
				who = "company"
			}
			fmt.Fprintf(opts.Stdout, "  verification %s  %s  %s  %s\n", v.Id.String(), name, who, v.State)
		}
	}
	if body.Incomplete {
		fmt.Fprintln(opts.Stdout, "\nSome offers did not load; the list may be incomplete. Try again later.")
	}
	return nil
}

// cents prints an amount in cents as dollars.
func cents(c int) string {
	return fmt.Sprintf("$%d.%02d", c/100, c%100)
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
	if n.SubjectType != nil {
		kind := string(*n.SubjectType)
		if kind == "business" {
			kind = "company"
		}
		if n.SubjectName != nil && *n.SubjectName != "" {
			fmt.Fprintf(opts.Stdout, "  Verified for: %s (%s)\n", *n.SubjectName, kind)
		} else {
			fmt.Fprintf(opts.Stdout, "  Verified as:  %s\n", kind)
		}
	}
	if n.VoiceAgentId != nil {
		fmt.Fprintf(opts.Stdout, "  Calls:        agent %s\n", n.VoiceAgentId.String())
	}
	if n.SmsAgentId != nil {
		fmt.Fprintf(opts.Stdout, "  Texts:        agent %s\n", n.SmsAgentId.String())
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

// --------------------------------------------------------------------------- //
// register-sms
// --------------------------------------------------------------------------- //

type numberRegisterSmsFlags struct {
	subject  string
	numberID string
	fields   []string
	address  []string
}

func newNumberRegisterSmsCmd(opts *Options) *cobra.Command {
	f := &numberRegisterSmsFlags{}
	cmd := &cobra.Command{
		Use:   "register-sms",
		Short: "Register to send SMS from US numbers (A2P 10DLC brand and campaign)",
		Long: `hail numbers register-sms — file your US SMS registration.

Posts purpose=sms to POST /v1/verifications for a US local number. The carrier
fee is debited from prepaid credit at filing. Required fields and fees:
GET /v1/verifications/requirements?purpose=sms&country_code=US&number_type=local&subject_type=business
and GET /v1/verifications/sms-fees. --subject-type is required: person (you)
or business (your company).`,
		Example: `  hail numbers register-sms --subject-type business \
    --field legal_name="Acme Inc" --field ein=12-3456789 \
    --address customer_name="Acme Inc" --address street="1 Main St" \
    --address city=Austin --address region=TX --address postal_code=78701`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			if f.subject != "person" && f.subject != "business" {
				return fmt.Errorf("--subject-type is required: person or business")
			}
			fields, err := parseKeyValues(f.fields, "--field")
			if err != nil {
				return err
			}
			address, err := parseKeyValues(f.address, "--address")
			if err != nil {
				return err
			}
			if f.numberID != "" {
				if _, err := uuid.Parse(f.numberID); err != nil {
					return fmt.Errorf("--number-id must be a full UUID")
				}
			}
			body, contentType, err := buildRegisterSmsBody(f.numberID, f.subject, fields, address)
			if err != nil {
				return err
			}
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			httpResp, err := apiClient.CreateVerification(
				cmd.Context(), &client.CreateVerificationParams{}, func(c context.Context, r *http.Request) error {
					r.Header.Set("Content-Type", contentType)
					r.Body = io.NopCloser(bytes.NewReader(body))
					r.ContentLength = int64(len(body))
					return nil
				},
			)
			if err != nil {
				return fmt.Errorf("verifications API: %w", err)
			}
			defer httpResp.Body.Close()
			raw, err := io.ReadAll(httpResp.Body)
			if err != nil {
				return fmt.Errorf("verifications API: %w", err)
			}
			if httpResp.StatusCode < 200 || httpResp.StatusCode > 299 {
				return apiError(httpResp.StatusCode, raw)
			}
			var v client.VerificationResponse
			if err := json.Unmarshal(raw, &v); err != nil {
				return fmt.Errorf("decode verification: %w", err)
			}
			if opts.JSON {
				return printJSON(opts.Stdout, v)
			}
			fmt.Fprintf(opts.Stdout, "✓ SMS registration %s: %s\n", v.Id.String(), v.State)
			return nil
		},
	}
	cmd.Flags().StringVar(&f.subject, "subject-type", "", "Required. Who registers: person (you) or business (your company)")
	// Old name of --subject-type, kept so existing scripts still work.
	cmd.Flags().StringVar(&f.subject, "subject", "", "Deprecated: use --subject-type")
	_ = cmd.Flags().MarkDeprecated("subject", "use --subject-type")
	cmd.Flags().StringVar(&f.numberID, "number-id", "", "Id of the US number to register (from `hail numbers list`). Needed when your US numbers are with more than one carrier.")
	cmd.Flags().StringArrayVar(&f.fields, "field", nil, "Registration field as key=value (repeatable)")
	cmd.Flags().StringArrayVar(&f.address, "address", nil, "Address part as key=value (repeatable): street, city, region, postal_code, customer_name")
	return cmd
}

// parseKeyValues turns repeated key=value flags into a map.
func parseKeyValues(pairs []string, flag string) (map[string]string, error) {
	out := make(map[string]string, len(pairs))
	for _, p := range pairs {
		k, v, ok := strings.Cut(p, "=")
		if !ok || k == "" {
			return nil, fmt.Errorf("%s %q must be key=value", flag, p)
		}
		out[k] = v
	}
	return out, nil
}

// buildRegisterSmsBody builds the multipart body for POST /v1/verifications
// with purpose=sms. The address always carries country_code US.
func buildRegisterSmsBody(numberID, subject string, fields, address map[string]string) ([]byte, string, error) {
	addr := make(map[string]string, len(address)+1)
	for k, v := range address {
		addr[k] = v
	}
	addr["country_code"] = "US"
	fieldsJSON, err := json.Marshal(fields)
	if err != nil {
		return nil, "", err
	}
	addressJSON, err := json.Marshal(addr)
	if err != nil {
		return nil, "", err
	}
	var buf bytes.Buffer
	w := multipart.NewWriter(&buf)
	for _, kv := range [][2]string{
		{"purpose", "sms"},
		{"country_code", "US"},
		{"number_type", "local"},
		{"subject_type", subject},
		{"fields", string(fieldsJSON)},
		{"address", string(addressJSON)},
	} {
		if err := w.WriteField(kv[0], kv[1]); err != nil {
			return nil, "", fmt.Errorf("build request: %w", err)
		}
	}
	if numberID != "" {
		if err := w.WriteField("number_id", numberID); err != nil {
			return nil, "", fmt.Errorf("build request: %w", err)
		}
	}
	if err := w.Close(); err != nil {
		return nil, "", fmt.Errorf("build request: %w", err)
	}
	return buf.Bytes(), w.FormDataContentType(), nil
}

// --------------------------------------------------------------------------- //
// confirm-sms
// --------------------------------------------------------------------------- //

func newNumberConfirmSmsCmd(opts *Options) *cobra.Command {
	var code string
	cmd := &cobra.Command{
		Use:   "confirm-sms <verification-id> --code <code>",
		Short: "Enter the code the carrier texted to confirm a person's US SMS registration",
		Long: `hail numbers confirm-sms — confirm a person's US SMS registration.

Use it when the registration shows confirmation "code" (hail --json numbers
register-sms, or GET /v1/verifications/{id}). Posts the code the carrier
texted to the person's mobile to POST /v1/verifications/{id}/confirm.`,
		Example: `  hail numbers confirm-sms 11111111-1111-1111-1111-111111111111 --code 482913`,
		Args:    argsOrHelp(1, "<verification-id>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			id, err := uuid.Parse(args[0])
			if err != nil {
				return fmt.Errorf("verification id must be a full UUID")
			}
			code = strings.TrimSpace(code)
			if len(code) < 4 || len(code) > 10 {
				return fmt.Errorf("--code must be the 4 to 10 characters the carrier texted")
			}
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			resp, err := apiClient.ConfirmVerificationWithResponse(
				cmd.Context(),
				openapi_types.UUID(id),
				&client.ConfirmVerificationParams{},
				client.ConfirmVerificationJSONRequestBody{Code: code},
			)
			if err != nil {
				return fmt.Errorf("verifications API: %w", err)
			}
			if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
				return apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			if opts.JSON {
				return printJSON(opts.Stdout, resp.JSON200)
			}
			fmt.Fprintf(opts.Stdout, "✓ SMS registration %s: %s\n", resp.JSON200.Id.String(), resp.JSON200.State)
			return nil
		},
	}
	cmd.Flags().StringVar(&code, "code", "", "The code the carrier texted (4 to 10 characters)")
	return cmd
}

// --------------------------------------------------------------------------- //
// cancel-sms
// --------------------------------------------------------------------------- //

func newNumberCancelSmsCmd(opts *Options) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "cancel-sms <verification-id>",
		Short: "Cancel a US SMS registration (stops its monthly fee)",
		Long: `hail numbers cancel-sms — cancel a US SMS registration.

Calls DELETE /v1/verifications/{id}. An approved registration is cancelled at
once: its numbers leave the campaign and US-to-US texts from them return 422.
The monthly fee is charged through the month of the cancellation, then stops.
Telnyx bills at least 3 monthly fees from approval, so a Telnyx registration
cancelled sooner is still charged for 3 months. A registration not yet sent
for review, or a rejected one, is withdrawn.`,
		Example: `  hail numbers cancel-sms 11111111-1111-1111-1111-111111111111`,
		Args:    argsOrHelp(1, "<verification-id>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			id, err := uuid.Parse(args[0])
			if err != nil {
				return fmt.Errorf("verification id must be a full UUID")
			}
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			resp, err := apiClient.CancelVerificationWithResponse(
				cmd.Context(),
				openapi_types.UUID(id),
				&client.CancelVerificationParams{},
			)
			if err != nil {
				return fmt.Errorf("verifications API: %w", err)
			}
			if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
				return apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			if opts.JSON {
				return printJSON(opts.Stdout, resp.JSON200)
			}
			fmt.Fprintf(opts.Stdout, "✓ SMS registration %s: %s\n", resp.JSON200.Id.String(), resp.JSON200.State)
			return nil
		},
	}
	return cmd
}
