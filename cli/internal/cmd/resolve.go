package cmd

import (
	"context"
	"encoding/hex"
	"fmt"
	"net/http"
	"strings"

	"github.com/google/uuid"
	openapi_types "github.com/oapi-codegen/runtime/types"

	"github.com/hail-hq/hail/cli/internal/client"
)

const recentPrefixWindow = 200

func isHex(s string) bool {
	for _, c := range s {
		switch {
		case c >= '0' && c <= '9':
		case c >= 'a' && c <= 'f':
		default:
			return false
		}
	}
	return true
}

// resolveIDPrefix turns a CLI-supplied id (full UUID or 4+ char hex
// prefix) into a full UUID via `lister`. Mirrors `git rev-parse`'s
// short-hash UX. `label` is the noun used in error messages.
func resolveIDPrefix[T any](
	ctx context.Context,
	input, label string,
	lister func(ctx context.Context, limit int) ([]T, error),
	getID func(T) openapi_types.UUID,
) (uuid.UUID, *T, error) {
	if parsed, err := uuid.Parse(input); err == nil {
		return parsed, nil, nil
	}
	needle := strings.ToLower(strings.ReplaceAll(input, "-", ""))
	if len(needle) < 4 || len(needle) > 31 || !isHex(needle) {
		return uuid.Nil, nil, fmt.Errorf("invalid %s id %q (expected full UUID or 4+ char hex prefix)", label, input)
	}

	items, err := lister(ctx, recentPrefixWindow)
	if err != nil {
		return uuid.Nil, nil, fmt.Errorf("resolve %s prefix: %w", label, err)
	}

	var matches []*T
	for i := range items {
		id := getID(items[i])
		if strings.HasPrefix(hex.EncodeToString(id[:]), needle) {
			matches = append(matches, &items[i])
		}
	}

	switch len(matches) {
	case 1:
		return uuid.UUID(getID(*matches[0])), matches[0], nil
	case 0:
		return uuid.Nil, nil, fmt.Errorf("no %s matches prefix %q (searched %d recent rows); pass the full UUID for older rows", label, input, len(items))
	default:
		names := make([]string, 0, len(matches))
		for _, m := range matches {
			names = append(names, uuid.UUID(getID(*m)).String())
		}
		return uuid.Nil, nil, fmt.Errorf("ambiguous prefix %q matches %d %ss: %s", input, len(matches), label, strings.Join(names, ", "))
	}
}

func resolveCallID(ctx context.Context, apiClient *client.ClientWithResponses, input string) (uuid.UUID, *client.CallResponse, error) {
	return resolveIDPrefix(ctx, input, "call",
		func(ctx context.Context, limit int) ([]client.CallResponse, error) {
			resp, err := apiClient.ListCallsCallsGetWithResponse(ctx, &client.ListCallsCallsGetParams{Limit: &limit})
			if err != nil {
				return nil, err
			}
			if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
				return nil, apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			return resp.JSON200.Items, nil
		},
		func(c client.CallResponse) openapi_types.UUID { return c.Id },
	)
}

func resolveEmailID(ctx context.Context, apiClient *client.ClientWithResponses, input string) (uuid.UUID, error) {
	id, _, err := resolveIDPrefix(ctx, input, "email",
		func(ctx context.Context, limit int) ([]client.EmailSummary, error) {
			resp, err := apiClient.ListEmailsEmailsGetWithResponse(ctx, &client.ListEmailsEmailsGetParams{Limit: &limit})
			if err != nil {
				return nil, err
			}
			if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
				return nil, apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			return resp.JSON200.Items, nil
		},
		func(e client.EmailSummary) openapi_types.UUID { return e.Id },
	)
	return id, err
}

func resolveEmailDomainID(ctx context.Context, apiClient *client.ClientWithResponses, input string) (uuid.UUID, error) {
	id, _, err := resolveIDPrefix(ctx, input, "email-domain",
		func(ctx context.Context, limit int) ([]client.EmailDomainResponse, error) {
			resp, err := apiClient.ListEmailDomainsEmailDomainsGetWithResponse(ctx, &client.ListEmailDomainsEmailDomainsGetParams{Limit: &limit})
			if err != nil {
				return nil, err
			}
			if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
				return nil, apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			return resp.JSON200.Items, nil
		},
		func(d client.EmailDomainResponse) openapi_types.UUID { return d.Id },
	)
	return id, err
}

func resolveWebhookID(ctx context.Context, apiClient *client.ClientWithResponses, input string) (uuid.UUID, error) {
	id, _, err := resolveIDPrefix(ctx, input, "webhook",
		func(ctx context.Context, limit int) ([]client.WebhookSubscriptionResponse, error) {
			resp, err := apiClient.ListSubscriptionsWebhooksGetWithResponse(ctx, &client.ListSubscriptionsWebhooksGetParams{Limit: &limit})
			if err != nil {
				return nil, err
			}
			if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
				return nil, apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			return resp.JSON200.Items, nil
		},
		func(s client.WebhookSubscriptionResponse) openapi_types.UUID { return s.Id },
	)
	return id, err
}

func resolveWebhookDeliveryID(ctx context.Context, apiClient *client.ClientWithResponses, subID uuid.UUID, input string) (uuid.UUID, error) {
	id, _, err := resolveIDPrefix(ctx, input, "delivery",
		func(ctx context.Context, limit int) ([]client.WebhookDeliveryResponse, error) {
			resp, err := apiClient.ListDeliveriesWebhooksSubIdDeliveriesGetWithResponse(
				ctx,
				openapi_types.UUID(subID),
				&client.ListDeliveriesWebhooksSubIdDeliveriesGetParams{Limit: &limit},
			)
			if err != nil {
				return nil, err
			}
			if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
				return nil, apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			return resp.JSON200.Items, nil
		},
		func(d client.WebhookDeliveryResponse) openapi_types.UUID { return d.Id },
	)
	return id, err
}
