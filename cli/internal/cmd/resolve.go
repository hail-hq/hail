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

// matchHexPrefix scans an already-fetched slice for items whose UUID's hex
// (no dashes) begins with `input` (also dash-stripped + lowercased).
// Validates the input shape (full UUID short-circuits; 4..31 char hex
// prefix only); returns the unique match, an ambiguous-prefix error, or a
// no-match error. `label` is the noun used in error messages.
//
// Two callers: `resolveIDPrefix` (fetches a recent page first), and
// `matchAttachmentPrefix` (searches a parent email's inline attachment
// list — no separate list endpoint exists).
func matchHexPrefix[T any](
	items []T,
	getID func(T) openapi_types.UUID,
	input, label string,
) (uuid.UUID, *T, error) {
	if parsed, err := uuid.Parse(input); err == nil {
		for i := range items {
			if uuid.UUID(getID(items[i])) == parsed {
				return parsed, &items[i], nil
			}
		}
		// Full UUID, not in the fetched set — let the caller decide if this
		// is a hard error (attachment) or a "fall through to direct GET"
		// case (resolveIDPrefix short-circuits before calling us in that path).
		return parsed, nil, nil
	}
	needle := strings.ToLower(strings.ReplaceAll(input, "-", ""))
	if len(needle) < 4 || len(needle) > 31 || !isHex(needle) {
		return uuid.Nil, nil, fmt.Errorf("invalid %s id %q (expected full UUID or 4+ char hex prefix)", label, input)
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
		return uuid.Nil, nil, fmt.Errorf("no %s matches prefix %q (searched %d rows); pass the full UUID for older rows", label, input, len(items))
	default:
		names := make([]string, 0, len(matches))
		for _, m := range matches {
			names = append(names, uuid.UUID(getID(*m)).String())
		}
		return uuid.Nil, nil, fmt.Errorf("ambiguous prefix %q matches %d %ss: %s", input, len(matches), label, strings.Join(names, ", "))
	}
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
	// Full UUID short-circuits — no list roundtrip needed.
	if parsed, err := uuid.Parse(input); err == nil {
		return parsed, nil, nil
	}
	// Validate shape locally before paying for a list roundtrip; malformed
	// inputs should fail without an HTTP call.
	needle := strings.ToLower(strings.ReplaceAll(input, "-", ""))
	if len(needle) < 4 || len(needle) > 31 || !isHex(needle) {
		return uuid.Nil, nil, fmt.Errorf("invalid %s id %q (expected full UUID or 4+ char hex prefix)", label, input)
	}
	items, err := lister(ctx, recentPrefixWindow)
	if err != nil {
		return uuid.Nil, nil, fmt.Errorf("resolve %s prefix: %w", label, err)
	}
	return matchHexPrefix(items, getID, input, label)
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

func resolveNumberID(ctx context.Context, apiClient *client.ClientWithResponses, input string) (uuid.UUID, error) {
	id, _, err := resolveIDPrefix(ctx, input, "number",
		func(ctx context.Context, limit int) ([]client.PhoneNumberResponse, error) {
			resp, err := apiClient.ListNumbersNumbersGetWithResponse(ctx, &client.ListNumbersNumbersGetParams{Limit: &limit})
			if err != nil {
				return nil, err
			}
			if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
				return nil, apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			return resp.JSON200.Items, nil
		},
		func(n client.PhoneNumberResponse) openapi_types.UUID { return n.Id },
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
