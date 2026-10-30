package cmd

import (
	"encoding/json"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/hail-hq/hail/cli/internal/client"
)

func sampleEmailSuppression(idStr, recipient string) client.SuppressionResponse {
	s := sampleSuppression(idStr, recipient, "unsubscribed", "unsubscribe_link")
	s.Channel = "email"
	return s
}

func TestEmailSuppressionsList_RendersRows(t *testing.T) {
	items := []client.SuppressionResponse{
		sampleEmailSuppression("11111111-1111-1111-1111-111111111111", "alice@example.com"),
		sampleEmailSuppression("22222222-2222-2222-2222-222222222221", "bob@example.com"),
	}
	next := "cur_2"
	srv := newFakeServer(t, http.StatusOK, client.SuppressionListResponse{Items: items, NextCursor: &next})

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "suppressions", "list", "--limit", "2",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if srv.lastReq.Method != http.MethodGet || srv.lastReq.URL.Path != "/v1/emails/suppressions" {
		t.Fatalf("unexpected route: %s %s", srv.lastReq.Method, srv.lastReq.URL.Path)
	}
	if got := srv.lastReq.URL.Query().Get("limit"); got != "2" {
		t.Errorf("?limit = %q, want 2", got)
	}
	for _, want := range []string{"alice@example.com", "bob@example.com", "unsubscribed", "unsubscribe_link", "--cursor cur_2"} {
		if !strings.Contains(stdout, want) {
			t.Errorf("missing %q in stdout:\n%s", want, stdout)
		}
	}
}

func TestEmailSuppressionsList_Empty(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, client.SuppressionListResponse{Items: nil})

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "suppressions", "list",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if strings.TrimSpace(stdout) != "" {
		t.Errorf("expected no rows, got: %q", stdout)
	}
}

func TestEmailSuppressionsList_JSONOutput(t *testing.T) {
	items := []client.SuppressionResponse{
		sampleEmailSuppression("11111111-1111-1111-1111-111111111111", "alice@example.com"),
	}
	srv := newFakeServer(t, http.StatusOK, client.SuppressionListResponse{Items: items})

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"--json", "email", "suppressions", "list",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var got client.SuppressionListResponse
	if err := json.Unmarshal([]byte(stdout), &got); err != nil {
		t.Fatalf("stdout is not valid JSON: %v\n%s", err, stdout)
	}
	if len(got.Items) != 1 || got.Items[0].Recipient != "alice@example.com" {
		t.Errorf("unexpected JSON payload: %+v", got)
	}
}

func TestEmailSuppressionsDelete_HappyPath(t *testing.T) {
	srv := newFakeServer(t, http.StatusNoContent, nil)

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "suppressions", "delete", "alice@example.com",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if srv.lastReq.Method != http.MethodDelete || srv.lastReq.URL.Path != "/v1/emails/suppressions/alice@example.com" {
		t.Fatalf("unexpected route: %s %s", srv.lastReq.Method, srv.lastReq.URL.Path)
	}
	if !strings.Contains(stdout, "Removed alice@example.com") {
		t.Errorf("stdout missing confirmation: %q", stdout)
	}
}

func TestEmailSuppressionsDelete_NotFound(t *testing.T) {
	srv := newFakeServer(t, http.StatusNotFound, map[string]string{"detail": "suppression not found"})

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "suppressions", "delete", "alice@example.com",
	)
	if err == nil {
		t.Fatal("expected error, got nil")
	}
	if !strings.Contains(err.Error(), "not found") {
		t.Errorf("error = %v", err)
	}
}

func TestEmailSuppressionsDelete_MissingArg(t *testing.T) {
	srv := newFakeServer(t, http.StatusNoContent, nil)

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "suppressions", "delete",
	)
	if err == nil {
		t.Fatal("expected error")
	}
	if hits := atomic.LoadInt32(&srv.hits); hits != 0 {
		t.Errorf("expected 0 HTTP calls, got %d", hits)
	}
}
