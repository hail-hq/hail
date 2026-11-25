package cmd

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"text/tabwriter"

	"github.com/google/uuid"
	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/hail-hq/hail/cli/internal/client"
)

// newSipConnectionsCmd builds `hail sip-connections`: connect your own
// phone system (PBX) so its calls reach Hail agents.
func newSipConnectionsCmd(opts *Options) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "sip-connections",
		Short: "Connect your phone system (PBX) to Hail agents",
		// Hidden from help while the API opens these routes to staff only.
		Hidden: true,
		Long: `hail sip-connections: connect your own phone system (PBX).

1. create        gives the SIP server, username and password for a generic
                 SIP trunk on your phone system.
2. add-number    adds a number your phone system holds (state unverified).
3. verify        Hail calls the number to prove you own it (up to 2 minutes,
                 billed as a normal outbound call).
4. hail numbers update <number_id> --voice-agent <agent_id>

Example:
  hail sip-connections create --label "Main office" --region US --pbx-host pbx.example.com
  hail sip-connections add-number <connection_id> +14155550100
  hail sip-connections verify <connection_id> <number_id>`,
	}
	cmd.AddCommand(newSipConnectionsCreateCmd(opts))
	cmd.AddCommand(newSipConnectionsListCmd(opts))
	cmd.AddCommand(newSipConnectionsGetCmd(opts))
	cmd.AddCommand(newSipConnectionsUpdateCmd(opts))
	cmd.AddCommand(newSipConnectionsRotateCmd(opts))
	cmd.AddCommand(newSipConnectionsDeleteCmd(opts))
	cmd.AddCommand(newSipConnectionsAddNumberCmd(opts))
	cmd.AddCommand(newSipConnectionsVerifyCmd(opts))
	cmd.AddCommand(newSipConnectionsRemoveNumberCmd(opts))
	return cmd
}

func parseUUIDArg(value, name string) (openapi_types.UUID, error) {
	parsed, err := uuid.Parse(value)
	if err != nil {
		return openapi_types.UUID{}, fmt.Errorf("%s must be a UUID: %q", name, value)
	}
	return openapi_types.UUID(parsed), nil
}

// --------------------------------------------------------------------------- //
// create
// --------------------------------------------------------------------------- //

type sipCreateFlags struct {
	label, region, host, transport, username, password string
	port                                               int
}

func newSipConnectionsCreateCmd(opts *Options) *cobra.Command {
	f := &sipCreateFlags{}
	cmd := &cobra.Command{
		Use:   "create",
		Short: "Connect a phone system; prints the SIP trunk settings",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			if f.label == "" || f.region == "" {
				return requireInputs(cmd, "--label and --region")
			}
			password, err := readSecretFlag(f.password, "--pbx-password", cmd.InOrStdin())
			if err != nil {
				return err
			}
			body := client.SipConnectionCreate{
				Label:         f.label,
				DefaultRegion: f.region,
				PbxHost:       strPtr(f.host),
				PbxUsername:   strPtr(f.username),
				PbxPassword:   strPtr(password),
			}
			if cmd.Flags().Changed("pbx-port") {
				body.PbxPort = &f.port
			}
			if f.transport != "" {
				t := client.SipConnectionCreatePbxTransport(f.transport)
				body.PbxTransport = &t
			}
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			resp, err := apiClient.CreateSipConnectionV1SipConnectionsPostWithResponse(
				cmd.Context(), &client.CreateSipConnectionV1SipConnectionsPostParams{}, body)
			if err != nil {
				return fmt.Errorf("sip-connections API: %w", err)
			}
			if resp.HTTPResponse.StatusCode != http.StatusCreated || resp.JSON201 == nil {
				return apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			if opts.JSON {
				return printJSON(opts.Stdout, resp.JSON201)
			}
			c := resp.JSON201
			fmt.Fprintln(opts.Stderr, "Put the password in your phone system's trunk. Staff can read it again in the console.")
			fmt.Fprintf(opts.Stdout, "✓ Connection %s: %s\n", c.Label, c.Id)
			fmt.Fprintf(opts.Stdout, "  SIP server:    %s\n", c.SipHost)
			fmt.Fprintf(opts.Stdout, "  Username:      %s\n", c.Username)
			fmt.Fprintf(opts.Stdout, "  Password:      %s\n", c.Password)
			fmt.Fprintln(opts.Stdout, "  Audio:         G.711 (PCMU or PCMA)")
			fmt.Fprintln(opts.Stdout, "  Number format: +country code, like +14155550100")
			fmt.Fprintln(opts.Stdout, "  Key presses:   RFC 2833")
			return nil
		},
	}
	cmd.Flags().StringVar(&f.label, "label", "", "Name, such as \"Main office\" (required)")
	cmd.Flags().StringVar(&f.region, "region", "", "2-letter country of the phone system, such as US (required)")
	cmd.Flags().StringVar(&f.host, "pbx-host", "", "Public address Hail dials for calls out and handover")
	cmd.Flags().IntVar(&f.port, "pbx-port", 5060, "SIP port of the phone system")
	cmd.Flags().StringVar(&f.transport, "pbx-transport", "", "auto, udp, tcp or tls (default auto)")
	cmd.Flags().StringVar(&f.username, "pbx-username", "", "Username Hail sends when the phone system asks")
	cmd.Flags().StringVar(&f.password, "pbx-password", "", "Password Hail sends with --pbx-username; \"-\" reads it from stdin (keeps it out of shell history)")
	return cmd
}

// --------------------------------------------------------------------------- //
// list / get
// --------------------------------------------------------------------------- //

func newSipConnectionsListCmd(opts *Options) *cobra.Command {
	var all bool
	var cursor string
	var limit int
	cmd := &cobra.Command{
		Use:     "list",
		Aliases: []string{"ls"},
		Short:   "List connected phone systems",
		Args:    cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			items, next, err := walkCursor(all, cursor, opts.Stderr, "connections",
				func(c string) (cursorPage[client.SipConnectionResponse], error) {
					resp, err := apiClient.ListSipConnectionsV1SipConnectionsGetWithResponse(
						cmd.Context(),
						&client.ListSipConnectionsV1SipConnectionsGetParams{Cursor: strPtr(c), Limit: &limit},
					)
					if err != nil {
						return cursorPage[client.SipConnectionResponse]{}, fmt.Errorf("sip-connections API: %w", err)
					}
					if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
						return cursorPage[client.SipConnectionResponse]{}, apiError(resp.HTTPResponse.StatusCode, resp.Body)
					}
					return cursorPage[client.SipConnectionResponse]{items: resp.JSON200.Items, nextCursor: resp.JSON200.NextCursor}, nil
				})
			if err != nil {
				return err
			}
			if opts.JSON {
				return printJSON(opts.Stdout, client.SipConnectionListResponse{Items: items, NextCursor: next})
			}
			if len(items) == 0 {
				fmt.Fprintln(opts.Stdout, "(no connections)")
				return nil
			}
			w := tabwriter.NewWriter(opts.Stdout, 0, 0, 2, ' ', 0)
			fmt.Fprintln(w, "ID\tLABEL\tREGION\tPBX HOST")
			for _, c := range items {
				host := "-"
				if c.PbxHost != nil {
					host = *c.PbxHost
				}
				fmt.Fprintf(w, "%s\t%s\t%s\t%s\n", c.Id, c.Label, c.DefaultRegion, host)
			}
			return w.Flush()
		},
	}
	cmd.Flags().IntVar(&limit, "limit", 50, "Page size (1..100)")
	cmd.Flags().StringVar(&cursor, "cursor", "", "Resume from a previous next_cursor")
	cmd.Flags().BoolVar(&all, "all", false, "Walk every page")
	return cmd
}

func printSipConnection(opts *Options, c *client.SipConnectionResponse) error {
	if opts.JSON {
		return printJSON(opts.Stdout, c)
	}
	fmt.Fprintf(opts.Stdout, "Connection %s: %s\n", c.Label, c.Id)
	fmt.Fprintf(opts.Stdout, "  SIP server: %s\n", c.SipHost)
	fmt.Fprintf(opts.Stdout, "  Username:   %s\n", c.Username)
	fmt.Fprintf(opts.Stdout, "  Password:   ••••%s\n", c.PasswordLast4)
	fmt.Fprintf(opts.Stdout, "  Region:     %s\n", c.DefaultRegion)
	if c.PbxHost != nil {
		fmt.Fprintf(opts.Stdout, "  PBX:        %s:%d (%s)\n", *c.PbxHost, c.PbxPort, c.PbxTransport)
	} else {
		fmt.Fprintln(opts.Stdout, "  PBX:        none (answer only)")
	}
	return nil
}

func newSipConnectionsGetCmd(opts *Options) *cobra.Command {
	return &cobra.Command{
		Use:   "get <connection_id>",
		Short: "Show one connected phone system",
		Args:  argsOrHelp(1, "<connection_id>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			id, err := parseUUIDArg(args[0], "connection_id")
			if err != nil {
				return err
			}
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			resp, err := apiClient.GetSipConnectionV1SipConnectionsConnectionIdGetWithResponse(
				cmd.Context(), id, &client.GetSipConnectionV1SipConnectionsConnectionIdGetParams{})
			if err != nil {
				return fmt.Errorf("sip-connections API: %w", err)
			}
			if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
				return apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			return printSipConnection(opts, resp.JSON200)
		},
	}
}

// --------------------------------------------------------------------------- //
// update
// --------------------------------------------------------------------------- //

type sipUpdateFlags struct {
	label, host, transport, username, password string
	port                                       int
	clearHost, clearUsername                   bool
}

func newSipConnectionsUpdateCmd(opts *Options) *cobra.Command {
	f := &sipUpdateFlags{}
	cmd := &cobra.Command{
		Use:   "update <connection_id>",
		Short: "Change the label or where Hail dials your phone system",
		Long: `hail sip-connections update: flags left unset stay as they are.

--clear-pbx-host stops calls out and handover; calls in keep working.
A new host, port or transport needs --pbx-password again when a username is set.`,
		Args: argsOrHelp(1, "<connection_id>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			id, err := parseUUIDArg(args[0], "connection_id")
			if err != nil {
				return err
			}
			// A map, not client.SipConnectionPatch: a cleared field must be
			// sent as JSON null, which the generated omitempty type cannot.
			patch := map[string]any{}
			set := func(flag, key string, value any) {
				if cmd.Flags().Changed(flag) {
					patch[key] = value
				}
			}
			set("label", "label", f.label)
			set("pbx-host", "pbx_host", f.host)
			set("pbx-port", "pbx_port", f.port)
			set("pbx-transport", "pbx_transport", f.transport)
			set("pbx-username", "pbx_username", f.username)
			if cmd.Flags().Changed("pbx-password") {
				password, err := readSecretFlag(f.password, "--pbx-password", cmd.InOrStdin())
				if err != nil {
					return err
				}
				patch["pbx_password"] = password
			}
			if f.clearHost {
				patch["pbx_host"] = nil
			}
			if f.clearUsername {
				patch["pbx_username"] = nil
				patch["pbx_password"] = nil
			}
			if len(patch) == 0 {
				return requireInputs(cmd, "at least one flag")
			}
			raw, err := json.Marshal(patch)
			if err != nil {
				return err
			}
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			resp, err := apiClient.UpdateSipConnectionV1SipConnectionsConnectionIdPatchWithBodyWithResponse(
				cmd.Context(), id, &client.UpdateSipConnectionV1SipConnectionsConnectionIdPatchParams{},
				"application/json", bytes.NewReader(raw))
			if err != nil {
				return fmt.Errorf("sip-connections API: %w", err)
			}
			if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
				return apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			return printSipConnection(opts, resp.JSON200)
		},
	}
	cmd.Flags().StringVar(&f.label, "label", "", "New name")
	cmd.Flags().StringVar(&f.host, "pbx-host", "", "New public address of the phone system")
	cmd.Flags().BoolVar(&f.clearHost, "clear-pbx-host", false, "Remove the address (answer only)")
	cmd.Flags().IntVar(&f.port, "pbx-port", 5060, "New SIP port")
	cmd.Flags().StringVar(&f.transport, "pbx-transport", "", "auto, udp, tcp or tls")
	cmd.Flags().StringVar(&f.username, "pbx-username", "", "New username Hail sends")
	cmd.Flags().StringVar(&f.password, "pbx-password", "", "New password Hail sends; \"-\" reads it from stdin (keeps it out of shell history)")
	cmd.Flags().BoolVar(&f.clearUsername, "clear-pbx-username", false, "Remove the username and password")
	cmd.MarkFlagsMutuallyExclusive("pbx-host", "clear-pbx-host")
	cmd.MarkFlagsMutuallyExclusive("pbx-username", "clear-pbx-username")
	cmd.MarkFlagsMutuallyExclusive("pbx-password", "clear-pbx-username")
	return cmd
}

// --------------------------------------------------------------------------- //
// rotate-password / delete
// --------------------------------------------------------------------------- //

func newSipConnectionsRotateCmd(opts *Options) *cobra.Command {
	return &cobra.Command{
		Use:   "rotate-password <connection_id>",
		Short: "Make a new password; the old one stops working at once",
		Args:  argsOrHelp(1, "<connection_id>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			id, err := parseUUIDArg(args[0], "connection_id")
			if err != nil {
				return err
			}
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			resp, err := apiClient.RotateSipConnectionPasswordV1SipConnectionsConnectionIdRotatePasswordPostWithResponse(
				cmd.Context(), id, &client.RotateSipConnectionPasswordV1SipConnectionsConnectionIdRotatePasswordPostParams{})
			if err != nil {
				return fmt.Errorf("sip-connections API: %w", err)
			}
			if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
				return apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			if opts.JSON {
				return printJSON(opts.Stdout, resp.JSON200)
			}
			fmt.Fprintln(opts.Stderr, "The old password stopped working. Put this one in your phone system's trunk.")
			fmt.Fprintf(opts.Stdout, "Password: %s\n", resp.JSON200.Password)
			return nil
		},
	}
}

func newSipConnectionsDeleteCmd(opts *Options) *cobra.Command {
	return &cobra.Command{
		Use:     "delete <connection_id>",
		Aliases: []string{"rm"},
		Short:   "Delete a connection (remove its numbers first)",
		Args:    argsOrHelp(1, "<connection_id>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			id, err := parseUUIDArg(args[0], "connection_id")
			if err != nil {
				return err
			}
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			resp, err := apiClient.DeleteSipConnectionV1SipConnectionsConnectionIdDeleteWithResponse(
				cmd.Context(), id, &client.DeleteSipConnectionV1SipConnectionsConnectionIdDeleteParams{})
			if err != nil {
				return fmt.Errorf("sip-connections API: %w", err)
			}
			if resp.HTTPResponse.StatusCode != http.StatusNoContent {
				return apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			fmt.Fprintf(opts.Stdout, "✓ Connection deleted: %s\n", id)
			return nil
		},
	}
}

// --------------------------------------------------------------------------- //
// numbers: add / verify / remove
// --------------------------------------------------------------------------- //

func newSipConnectionsAddNumberCmd(opts *Options) *cobra.Command {
	return &cobra.Command{
		Use:   "add-number <connection_id> <+E164>",
		Short: "Add a number your phone system holds (starts unverified)",
		Args:  argsOrHelp(2, "<connection_id> <+E164>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			id, err := parseUUIDArg(args[0], "connection_id")
			if err != nil {
				return err
			}
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			resp, err := apiClient.AddSipConnectionNumberV1SipConnectionsConnectionIdNumbersPostWithResponse(
				cmd.Context(), id, &client.AddSipConnectionNumberV1SipConnectionsConnectionIdNumbersPostParams{},
				client.ConnectedNumberCreate{E164: args[1]})
			if err != nil {
				return fmt.Errorf("sip-connections API: %w", err)
			}
			if resp.HTTPResponse.StatusCode != http.StatusCreated || resp.JSON201 == nil {
				return apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			if err := printPhoneNumber(opts, resp.JSON201, true); err != nil {
				return err
			}
			if !opts.JSON {
				fmt.Fprintf(opts.Stderr, "Next: send this number's calls to Hail, then run\n  hail sip-connections verify %s %s\n", id, resp.JSON201.Id)
			}
			return nil
		},
	}
}

// readSecretFlag returns the flag's value; "-" reads the whole of stdin, so
// the secret stays out of shell history and the process table.
func readSecretFlag(flag, name string, stdin io.Reader) (string, error) {
	if flag != "-" {
		return flag, nil
	}
	raw, err := readAll(stdin)
	if err != nil {
		return "", fmt.Errorf("read %s from stdin: %w", name, err)
	}
	value := strings.TrimRight(raw, "\r\n")
	if value == "" {
		return "", fmt.Errorf("%s - given but stdin was empty", name)
	}
	return value, nil
}

// checkReasons turns a failed check's reason into the next step.
var checkReasons = map[client.ConnectedNumberCheckReason]string{
	"no_call_arrived":   "No call arrived. Check the SIP trunk and the routing rule.",
	"not_authenticated": "The call came in without this connection's username and password.",
	"code_not_received": "The check code did not come through. Turn on RFC 2833 key presses on the trunk.",
}

func newSipConnectionsVerifyCmd(opts *Options) *cobra.Command {
	return &cobra.Command{
		Use:   "verify <connection_id> <number_id>",
		Short: "Prove you own a number: Hail calls it (up to 2 minutes, billed)",
		Args:  argsOrHelp(2, "<connection_id> <number_id>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			connID, err := parseUUIDArg(args[0], "connection_id")
			if err != nil {
				return err
			}
			numberID, err := parseUUIDArg(args[1], "number_id")
			if err != nil {
				return err
			}
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			fmt.Fprintln(opts.Stderr, "Calling the number to check it. This can take up to 2 minutes.")
			// No client timeout: the API holds the request until the check ends.
			resp, err := apiClient.VerifySipConnectionNumberV1SipConnectionsConnectionIdNumbersNumberIdVerifyPostWithResponse(
				cmd.Context(), connID, numberID,
				&client.VerifySipConnectionNumberV1SipConnectionsConnectionIdNumbersNumberIdVerifyPostParams{})
			if err != nil {
				return fmt.Errorf("sip-connections API: %w", err)
			}
			if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
				return apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			return reportCheck(opts, resp.JSON200)
		},
	}
}

func reportCheck(opts *Options, r *client.ConnectedNumberCheck) error {
	if opts.JSON {
		if err := printJSON(opts.Stdout, r); err != nil {
			return err
		}
	}
	if r.State == "active" {
		if !opts.JSON {
			fmt.Fprintln(opts.Stdout, "✓ Connected")
		}
		return nil
	}
	msg := "The check did not pass."
	if r.Reason != nil {
		if text, ok := checkReasons[*r.Reason]; ok {
			msg = text
		}
	}
	return fmt.Errorf("%s", msg)
}

func newSipConnectionsRemoveNumberCmd(opts *Options) *cobra.Command {
	return &cobra.Command{
		Use:   "remove-number <connection_id> <number_id>",
		Short: "Remove a number from Hail (it stays with your phone system)",
		Args:  argsOrHelp(2, "<connection_id> <number_id>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			connID, err := parseUUIDArg(args[0], "connection_id")
			if err != nil {
				return err
			}
			numberID, err := parseUUIDArg(args[1], "number_id")
			if err != nil {
				return err
			}
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			resp, err := apiClient.RemoveSipConnectionNumberV1SipConnectionsConnectionIdNumbersNumberIdDeleteWithResponse(
				cmd.Context(), connID, numberID,
				&client.RemoveSipConnectionNumberV1SipConnectionsConnectionIdNumbersNumberIdDeleteParams{})
			if err != nil {
				return fmt.Errorf("sip-connections API: %w", err)
			}
			if resp.HTTPResponse.StatusCode != http.StatusNoContent {
				return apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			fmt.Fprintf(opts.Stdout, "✓ Number removed from Hail: %s\n", numberID)
			return nil
		},
	}
}
