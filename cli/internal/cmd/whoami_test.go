package cmd

import (
	"encoding/json"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
)

func TestWhoami_PrintsEmailOrgAndAuthKind(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, map[string]any{
		"auth_kind":       "apikey",
		"organization_id": "22222222-2222-2222-2222-222222222222",
		"user_id":         "11111111-1111-1111-1111-111111111111",
		"email":           "sarah@acme.test",
		"name":            "Sarah Chen",
	})

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"whoami",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got := atomic.LoadInt32(&srv.hits); got != 1 {
		t.Fatalf("expected 1 request, got %d", got)
	}
	if srv.lastReq.Method != http.MethodGet || srv.lastReq.URL.Path != "/whoami" {
		t.Fatalf("unexpected route: %s %s", srv.lastReq.Method, srv.lastReq.URL.Path)
	}
	for _, want := range []string{
		"sarah@acme.test",
		"Sarah Chen",
		"22222222-2222-2222-2222-222222222222",
		"apikey",
	} {
		if !strings.Contains(stdout, want) {
			t.Fatalf("stdout missing %q:\n%s", want, stdout)
		}
	}
}

func TestWhoami_SharedKeySaysNoUser(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, map[string]any{
		"auth_kind":       "shared",
		"organization_id": "22222222-2222-2222-2222-222222222222",
		"user_id":         nil,
		"email":           nil,
		"name":            nil,
	})

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"whoami",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(stdout, "shared operator key") {
		t.Fatalf("expected the no-user hint, got:\n%s", stdout)
	}
}

func TestWhoami_JSONPassesThrough(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, map[string]any{
		"auth_kind":       "jwt",
		"organization_id": "22222222-2222-2222-2222-222222222222",
		"user_id":         "11111111-1111-1111-1111-111111111111",
		"email":           "sarah@acme.test",
		"name":            "Sarah Chen",
	})

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"whoami", "--json",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var body map[string]any
	if err := json.Unmarshal([]byte(stdout), &body); err != nil {
		t.Fatalf("stdout is not JSON: %v\n%s", err, stdout)
	}
	if body["email"] != "sarah@acme.test" {
		t.Fatalf("email = %v", body["email"])
	}
}

// The default sender is printed even when the org owns no domains: that is
// the only place the hail-mail address a first send would mint shows up.
func TestEmailDomainList_PrintsDefaultFromOnEmptyList(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, map[string]any{
		"items":        []any{},
		"next_cursor":  nil,
		"default_from": "admin+acme@mail.hail.so",
	})

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "domain", "list",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(stdout, "admin+acme@mail.hail.so") {
		t.Fatalf("stdout missing the minted address:\n%s", stdout)
	}
}

func TestEmailDomainList_NullDefaultFromTellsCallerToPassFrom(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, map[string]any{
		"items": []any{
			map[string]any{
				"id":                  "33333333-3333-3333-3333-333333333333",
				"organization_id":     "22222222-2222-2222-2222-222222222222",
				"kind":                "custom",
				"domain":              "acme.com",
				"local_prefix_user":   nil,
				"local_prefix_org":    nil,
				"verification_status": "verified",
				"dns_records":         []any{},
				"mail_from_domain":    nil,
				"provider":            "ses",
				"verified_at":         nil,
				"created_at":          "2026-01-01T00:00:00Z",
				"updated_at":          "2026-01-01T00:00:00Z",
			},
		},
		"next_cursor":  nil,
		"default_from": nil,
	})

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "domain", "list",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(stdout, "a send without --from is rejected") {
		t.Fatalf("expected the explicit-from hint, got:\n%s", stdout)
	}
}
