// Webhook subscription management commands.
//
// hail webhooks create --url ... --events email.received
// hail webhooks list
// hail webhooks deliveries <subscription-id>
// hail webhooks redeliver <subscription-id> <delivery-id>
package cmd

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"text/tabwriter"

	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/hail-hq/hail/cli/internal/client"
)

func newWebhooksCmd(opts *Options) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "webhooks",
		Short: "Manage outbound webhook subscriptions",
		Long: `hail webhooks — org-wide outbound webhook subscriptions.

A subscription POSTs every matching event to your URL with an
HMAC-signed payload. Retries follow a fixed 0/30s/2m/10m/1h/6h/24h
ladder; after the last retry the delivery is marked 'dead' and after
50 consecutive dead deliveries the subscription auto-disables.

Subcommands:
  create       Register a subscription (returns the plaintext secret once).
  list         List subscriptions for the calling org.
  deliveries   Show recent delivery attempts for a subscription.
  redeliver    Replay a single delivery row.`,
	}
	cmd.AddCommand(newWebhooksCreateCmd(opts))
	cmd.AddCommand(newWebhooksListCmd(opts))
	cmd.AddCommand(newWebhooksDeliveriesCmd(opts))
	cmd.AddCommand(newWebhooksRedeliverCmd(opts))
	return cmd
}

type webhooksCreateFlags struct {
	url    string
	events string
}

func newWebhooksCreateCmd(opts *Options) *cobra.Command {
	f := &webhooksCreateFlags{}
	cmd := &cobra.Command{
		Use:   "create",
		Short: "Register a webhook subscription",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runWebhooksCreate(cmd.Context(), opts, f)
		},
	}
	cmd.Flags().StringVar(&f.url, "url", "", "Target URL (required)")
	cmd.Flags().StringVar(
		&f.events, "events", "email.received",
		"Comma-separated event types",
	)
	if err := cmd.MarkFlagRequired("url"); err != nil {
		panic(err)
	}
	return cmd
}

func runWebhooksCreate(ctx context.Context, opts *Options, f *webhooksCreateFlags) error {
	events := strings.Split(f.events, ",")
	body := client.WebhookSubscriptionCreate{
		TargetUrl: f.url,
	}
	for _, e := range events {
		e = strings.TrimSpace(e)
		if e == "" {
			continue
		}
		body.EventTypes = append(
			body.EventTypes, client.WebhookSubscriptionCreateEventTypes(e),
		)
	}
	if len(body.EventTypes) == 0 {
		return fmt.Errorf("--events must name at least one event type")
	}
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}
	resp, err := apiClient.CreateSubscriptionWebhooksPostWithResponse(
		ctx, &client.CreateSubscriptionWebhooksPostParams{}, body,
	)
	if err != nil {
		return fmt.Errorf("webhooks API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusCreated || resp.JSON201 == nil {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}
	return printJSON(opts.Stdout, resp.JSON201)
}

func newWebhooksListCmd(opts *Options) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List webhook subscriptions",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			resp, err := apiClient.ListSubscriptionsWebhooksGetWithResponse(
				cmd.Context(), &client.ListSubscriptionsWebhooksGetParams{},
			)
			if err != nil {
				return fmt.Errorf("webhooks API: %w", err)
			}
			if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
				return apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			return printWebhookSubscriptionList(opts, resp.JSON200)
		},
	}
	return cmd
}

func newWebhooksDeliveriesCmd(opts *Options) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "deliveries <subscription-id>",
		Short: "List delivery attempts for a subscription (full UUID or 4+ char prefix)",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			subID, err := resolveWebhookID(cmd.Context(), apiClient, args[0])
			if err != nil {
				return err
			}
			resp, err := apiClient.ListDeliveriesWebhooksSubIdDeliveriesGetWithResponse(
				cmd.Context(),
				openapi_types.UUID(subID),
				&client.ListDeliveriesWebhooksSubIdDeliveriesGetParams{},
			)
			if err != nil {
				return fmt.Errorf("webhooks API: %w", err)
			}
			if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
				return apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			return printWebhookDeliveryList(opts, resp.JSON200)
		},
	}
	return cmd
}

func newWebhooksRedeliverCmd(opts *Options) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "redeliver <subscription-id> <delivery-id>",
		Short: "Replay a single delivery row (each id may be full UUID or 4+ char prefix)",
		Args:  cobra.ExactArgs(2),
		RunE: func(cmd *cobra.Command, args []string) error {
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			subID, err := resolveWebhookID(cmd.Context(), apiClient, args[0])
			if err != nil {
				return err
			}
			deliveryID, err := resolveWebhookDeliveryID(cmd.Context(), apiClient, subID, args[1])
			if err != nil {
				return err
			}
			resp, err := apiClient.RedeliverWebhooksSubIdDeliveriesDeliveryIdRedeliverPostWithResponse(
				cmd.Context(),
				openapi_types.UUID(subID),
				openapi_types.UUID(deliveryID),
				&client.RedeliverWebhooksSubIdDeliveriesDeliveryIdRedeliverPostParams{},
			)
			if err != nil {
				return fmt.Errorf("webhooks API: %w", err)
			}
			if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
				return apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			// Redelivery state is machine-shaped (ids, retry ladder); emit
			// JSON unconditionally, like create.
			return printJSON(opts.Stdout, resp.JSON200)
		},
	}
	return cmd
}

// --------------------------------------------------------------------------- //
// printers
// --------------------------------------------------------------------------- //

func printWebhookSubscriptionList(opts *Options, body *client.WebhookSubscriptionListResponse) error {
	if opts.JSON {
		return printJSON(opts.Stdout, body)
	}
	if len(body.Items) == 0 {
		fmt.Fprintln(opts.Stdout, "(no webhook subscriptions)")
		return nil
	}
	w := tabwriter.NewWriter(opts.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(w, "ID\tURL\tEVENTS\tSTATUS\tFAILURES")
	for _, sub := range body.Items {
		fmt.Fprintf(
			w, "%s\t%s\t%s\t%s\t%d\n",
			sub.Id.String(),
			sub.TargetUrl,
			strings.Join(sub.EventTypes, ","),
			string(sub.Status),
			sub.ConsecutiveFailures,
		)
	}
	_ = w.Flush()
	if body.NextCursor != nil && *body.NextCursor != "" {
		fmt.Fprintf(opts.Stdout, "\nMore results; next_cursor: %s\n", *body.NextCursor)
	}
	return nil
}

func printWebhookDeliveryList(opts *Options, body *client.WebhookDeliveryListResponse) error {
	if opts.JSON {
		return printJSON(opts.Stdout, body)
	}
	if len(body.Items) == 0 {
		fmt.Fprintln(opts.Stdout, "(no deliveries)")
		return nil
	}
	w := tabwriter.NewWriter(opts.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(w, "ID\tEVENT\tSTATUS\tATTEMPT\tNEXT_ATTEMPT")
	for _, d := range body.Items {
		fmt.Fprintf(
			w, "%s\t%s\t%s\t%d\t%s\n",
			d.Id.String(),
			d.EventType,
			string(d.Status),
			d.Attempt,
			d.NextAttemptAt.UTC().Format(utcTSLayout),
		)
	}
	_ = w.Flush()
	if body.NextCursor != nil && *body.NextCursor != "" {
		fmt.Fprintf(opts.Stdout, "\nMore results; next_cursor: %s\n", *body.NextCursor)
	}
	return nil
}
