package cmd

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"text/tabwriter"

	"github.com/spf13/cobra"

	"github.com/hail-hq/hail/cli/internal/client"
)

// newContactsCmd builds the `contacts` subtree.
//
// A "contact" is the computed union the API exposes at GET /contacts: org
// members (their name/phone/email come from the membership row, id shaped
// "member:<user_id>") plus manually-saved rows (id is the row's own UUID,
// as a string). update/delete only make sense for manual rows; the API
// enforces that, this CLI does not special-case it — whatever error the
// server returns for a member: id surfaces via apiError like any other.
//
// A member's phone number is a distinct resource (PUT/DELETE
// /members/{user_id}/phone) — separate from the /contacts CRUD verbs
// because it's keyed by the org's own user id, not a contact id. The
// OpenAPI spec tags both groups "contacts", so set-phone/clear-phone hang
// off this same `hail contacts` parent rather than a new top-level
// `hail members` command.
func newContactsCmd(opts *Options) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "contacts",
		Short: "Manage the org's contact directory (members + manual contacts)",
		Long: `hail contacts — the org's contact directory.

GET /contacts returns a computed union of two kinds of row:
  member   an org member; name/phone/email come from their membership.
           id is "member:<user_id>".
  manual   a contact saved directly via 'hail contacts create'.
           id is the row's own UUID.

'update'/'delete' operate on manual rows. A member's own phone number is
managed separately via 'set-phone'/'clear-phone', since it lives on the
membership row, not a contacts row.`,
	}
	cmd.AddCommand(newContactsListCmd(opts))
	cmd.AddCommand(newContactsCreateCmd(opts))
	cmd.AddCommand(newContactsUpdateCmd(opts))
	cmd.AddCommand(newContactsDeleteCmd(opts))
	cmd.AddCommand(newContactsSetPhoneCmd(opts))
	cmd.AddCommand(newContactsClearPhoneCmd(opts))
	return cmd
}

// --------------------------------------------------------------------------- //
// list
// --------------------------------------------------------------------------- //

type contactsListFlags struct {
	q      string
	limit  int
	cursor string
	all    bool
}

func newContactsListCmd(opts *Options) *cobra.Command {
	f := &contactsListFlags{}
	cmd := &cobra.Command{
		Use:     "list",
		Aliases: []string{"ls"},
		Short:   "List the org's contacts (members + manual contacts)",
		Args:    cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runContactsList(cmd.Context(), opts, f)
		},
	}
	cmd.Flags().StringVar(&f.q, "q", "", "Filter by name/email/phone (server-side substring match)")
	cmd.Flags().IntVar(&f.limit, "limit", 100, "Page size (1..500)")
	cmd.Flags().StringVar(&f.cursor, "cursor", "", "Resume from a previous next_cursor")
	cmd.Flags().BoolVar(&f.all, "all", false, "Walk every page (warns at >1000 contacts)")
	return cmd
}

func runContactsList(ctx context.Context, opts *Options, f *contactsListFlags) error {
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}

	items, next, err := walkCursor(f.all, f.cursor, opts.Stderr, "contacts",
		func(cursor string) (cursorPage[client.ContactEntry], error) {
			params := &client.ListContactsContactsGetParams{
				Q:      strPtr(f.q),
				Cursor: strPtr(cursor),
				Limit:  &f.limit,
			}
			resp, err := apiClient.ListContactsContactsGetWithResponse(ctx, params)
			if err != nil {
				return cursorPage[client.ContactEntry]{}, fmt.Errorf("contacts API: %w", err)
			}
			if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
				return cursorPage[client.ContactEntry]{}, apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			return cursorPage[client.ContactEntry]{items: resp.JSON200.Items, nextCursor: resp.JSON200.NextCursor}, nil
		})
	if err != nil {
		return err
	}
	return printContactList(opts, &client.ContactListResponse{Items: items, NextCursor: next})
}

// --------------------------------------------------------------------------- //
// create
// --------------------------------------------------------------------------- //

type contactsCreateFlags struct {
	phone   string
	email   string
	idemKey string
}

func newContactsCreateCmd(opts *Options) *cobra.Command {
	f := &contactsCreateFlags{}
	cmd := &cobra.Command{
		Use:   "create <name>",
		Short: "Add a manual contact",
		Long: `hail contacts create — add a manual contact to the directory.

At least one of --phone or --email is required (the API rejects a contact
with neither).

Example:
  hail contacts create "Jane Doe" --phone +15551234567 --email jane@example.com`,
		Args: argsOrHelp(1, "<name>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runContactsCreate(cmd.Context(), cmd, opts, f, args[0])
		},
	}
	cmd.Flags().StringVar(&f.phone, "phone", "", "E.164 phone number — one of --phone/--email required")
	cmd.Flags().StringVar(&f.email, "email", "", "Email address — one of --phone/--email required")
	cmd.Flags().StringVar(&f.idemKey, "idempotency-key", "", "Defaults to a fresh UUID")
	markOneOfRequired(cmd, "contact-info", "phone", "email")
	return cmd
}

func runContactsCreate(ctx context.Context, cmd *cobra.Command, opts *Options, f *contactsCreateFlags, name string) error {
	// Belt-and-suspenders: markOneOfRequired only annotates --help output
	// (see root.go's oneOfRequiredAnnotation doc-comment); the API's own
	// 422 for "neither phone nor email" is mirrored here so the failure is
	// local and instant instead of a round trip.
	if f.phone == "" && f.email == "" {
		return requireInputs(cmd, "--phone or --email")
	}

	body := client.ContactCreate{
		Name:      name,
		PhoneE164: strPtr(f.phone),
		Email:     strPtr(f.email),
	}

	apiClient, err := opts.newClientWithIdempotency(f.idemKey)
	if err != nil {
		return err
	}

	resp, err := apiClient.CreateContactContactsPostWithResponse(ctx, &client.CreateContactContactsPostParams{}, body)
	if err != nil {
		return fmt.Errorf("contacts API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusCreated || resp.JSON201 == nil {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}

	return printContactEntry(opts, resp.JSON201, "created")
}

// --------------------------------------------------------------------------- //
// update
// --------------------------------------------------------------------------- //

type contactsUpdateFlags struct {
	name  string
	phone string
	email string
}

func newContactsUpdateCmd(opts *Options) *cobra.Command {
	f := &contactsUpdateFlags{}
	cmd := &cobra.Command{
		Use:   "update <id>",
		Short: "Update a manual contact's name, phone, or email",
		Long: `hail contacts update — patch one or more fields of an existing contact.

<id> is the id shown by 'hail contacts list' — a manual contact's UUID.
Flags left unset are unchanged.

Example:
  hail contacts update 11111111-1111-1111-1111-111111111111 --phone +15559876543`,
		Args: argsOrHelp(1, "<id>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runContactsUpdate(cmd.Context(), cmd, opts, f, args[0])
		},
	}
	cmd.Flags().StringVar(&f.name, "name", "", "New name")
	cmd.Flags().StringVar(&f.phone, "phone", "", "New E.164 phone number")
	cmd.Flags().StringVar(&f.email, "email", "", "New email address")
	return cmd
}

func runContactsUpdate(ctx context.Context, cmd *cobra.Command, opts *Options, f *contactsUpdateFlags, id string) error {
	// With no flags every ContactPatch field is nil/omitempty: the request
	// would be `{}`, the API would 200 the unchanged row, and the CLI would
	// print "✓ Contact updated" having changed nothing. Fail locally instead.
	if f.name == "" && f.phone == "" && f.email == "" {
		return requireInputs(cmd, "--name, --phone, or --email")
	}

	body := client.ContactPatch{
		Name:      strPtr(f.name),
		PhoneE164: strPtr(f.phone),
		Email:     strPtr(f.email),
	}

	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}

	resp, err := apiClient.PatchContactContactsContactIdPatchWithResponse(ctx, id, &client.PatchContactContactsContactIdPatchParams{}, body)
	if err != nil {
		return fmt.Errorf("contacts API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}
	return printContactEntry(opts, resp.JSON200, "updated")
}

// --------------------------------------------------------------------------- //
// delete
// --------------------------------------------------------------------------- //

// newContactsDeleteCmd deletes immediately, no --yes confirmation flag.
// Mirrors this codebase's existing destructive commands (`email domain
// delete`, `sms suppressions delete`) — neither prompts nor gates behind a
// confirmation flag, so contacts follows the same convention.
func newContactsDeleteCmd(opts *Options) *cobra.Command {
	return &cobra.Command{
		Use:     "delete <id>",
		Aliases: []string{"rm"},
		Short:   "Delete a manual contact",
		Args:    argsOrHelp(1, "<id>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runContactsDelete(cmd.Context(), opts, args[0])
		},
	}
}

func runContactsDelete(ctx context.Context, opts *Options, id string) error {
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}

	resp, err := apiClient.DeleteContactContactsContactIdDeleteWithResponse(ctx, id, &client.DeleteContactContactsContactIdDeleteParams{})
	if err != nil {
		return fmt.Errorf("contacts API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusNoContent {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}
	fmt.Fprintf(opts.Stdout, "✓ Contact %s deleted\n", id)
	return nil
}

// --------------------------------------------------------------------------- //
// set-phone / clear-phone — an org member's own phone number
// --------------------------------------------------------------------------- //

type contactsSetPhoneFlags struct {
	phone string
}

func newContactsSetPhoneCmd(opts *Options) *cobra.Command {
	f := &contactsSetPhoneFlags{}
	cmd := &cobra.Command{
		Use:   "set-phone <user-id|me>",
		Short: "Set an org member's phone number",
		Long: `hail contacts set-phone — set the phone number Hail calls/texts for an org member.

Pass 'me' to set your own membership row's number; otherwise pass the
member's user id.

Example:
  hail contacts set-phone me --phone +15551234567`,
		Args:    argsOrHelp(1, "<user-id|me>"),
		PreRunE: requireMarkedFlags,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runContactsSetPhone(cmd.Context(), opts, f, args[0])
		},
	}
	cmd.Flags().StringVar(&f.phone, "phone", "", "E.164 phone number")
	cmd.MarkFlagRequired("phone")
	return cmd
}

func runContactsSetPhone(ctx context.Context, opts *Options, f *contactsSetPhoneFlags, userID string) error {
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}

	body := client.MemberPhonePut{PhoneE164: f.phone}
	resp, err := apiClient.PutMemberPhoneMembersUserIdPhonePutWithResponse(ctx, userID, &client.PutMemberPhoneMembersUserIdPhonePutParams{}, body)
	if err != nil {
		return fmt.Errorf("members API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}

	if opts.JSON {
		return printJSON(opts.Stdout, resp.JSON200)
	}
	fmt.Fprintf(opts.Stdout, "✓ Phone set for %s: %s\n", userID, f.phone)
	return nil
}

func newContactsClearPhoneCmd(opts *Options) *cobra.Command {
	return &cobra.Command{
		Use:   "clear-phone <user-id|me>",
		Short: "Clear an org member's phone number",
		Long: `hail contacts clear-phone — remove the phone number from an org member's
membership row.

Pass 'me' to clear your own number; otherwise pass the member's user id.`,
		Args: argsOrHelp(1, "<user-id|me>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runContactsClearPhone(cmd.Context(), opts, args[0])
		},
	}
}

func runContactsClearPhone(ctx context.Context, opts *Options, userID string) error {
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}

	resp, err := apiClient.DeleteMemberPhoneMembersUserIdPhoneDeleteWithResponse(ctx, userID, &client.DeleteMemberPhoneMembersUserIdPhoneDeleteParams{})
	if err != nil {
		return fmt.Errorf("members API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusNoContent {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}
	fmt.Fprintf(opts.Stdout, "✓ Phone cleared for %s\n", userID)
	return nil
}

// --------------------------------------------------------------------------- //
// printers
// --------------------------------------------------------------------------- //

func printContactEntry(opts *Options, c *client.ContactEntry, verb string) error {
	if opts.JSON {
		return printJSON(opts.Stdout, c)
	}

	fmt.Fprintf(opts.Stdout, "✓ Contact %s: %s\n", verb, c.Id)
	fmt.Fprintf(opts.Stdout, "  Name:  %s\n", c.Name)
	fmt.Fprintf(opts.Stdout, "  Kind:  %s\n", string(c.Kind))
	if c.PhoneE164 != nil && *c.PhoneE164 != "" {
		fmt.Fprintf(opts.Stdout, "  Phone: %s\n", *c.PhoneE164)
	}
	if c.Email != nil && *c.Email != "" {
		fmt.Fprintf(opts.Stdout, "  Email: %s\n", *c.Email)
	}
	return nil
}

func printContactList(opts *Options, body *client.ContactListResponse) error {
	if opts.JSON {
		return printJSON(opts.Stdout, body)
	}
	if len(body.Items) == 0 {
		fmt.Fprintln(opts.Stdout, "(no contacts)")
		return nil
	}

	w := tabwriter.NewWriter(opts.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(w, "ID\tNAME\tKIND\tPHONE\tEMAIL")
	for _, c := range body.Items {
		phone := ""
		if c.PhoneE164 != nil {
			phone = *c.PhoneE164
		}
		email := ""
		if c.Email != nil {
			email = *c.Email
		}
		fmt.Fprintf(w, "%s\t%s\t%s\t%s\t%s\n", c.Id, c.Name, string(c.Kind), phone, email)
	}
	if err := w.Flush(); err != nil {
		return fmt.Errorf("write table: %w", err)
	}
	if body.NextCursor != nil && *body.NextCursor != "" {
		fmt.Fprintf(opts.Stdout, "\nmore: --cursor %s\n", strings.TrimSpace(*body.NextCursor))
	}
	return nil
}
