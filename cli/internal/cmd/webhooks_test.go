package cmd

import (
	"bytes"
	"encoding/json"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/google/uuid"
	openapi_types "github.com/oapi-codegen/runtime/types"

	"github.com/hail-hq/hail/cli/internal/client"
)

// TestWebhooksRegistered verifies that "hail webhooks" is registered as a
// top-level subcommand on the root command. This guards against the command
// tree being built but never wired into root.AddCommand.
func TestWebhooksRegistered(t *testing.T) {
	root := NewRootCmd(&bytes.Buffer{}, &bytes.Buffer{}, func(string) string { return "" })

	for _, sub := range root.Commands() {
		if sub.Name() == "webhooks" {
			return // found — pass
		}
	}
	t.Fatal("root command has no 'webhooks' subcommand; add root.AddCommand(newWebhooksCmd(opts)) in root.go")
}

func sampleWebhookSubscriptionResponse() client.WebhookSubscriptionResponse {
	id := openapi_types.UUID(uuid.MustParse("11111111-1111-1111-1111-111111111111"))
	orgID := openapi_types.UUID(uuid.MustParse("22222222-2222-2222-2222-222222222222"))
	now := time.Date(2026, 6, 10, 12, 0, 0, 0, time.UTC)
	secret := "whs_test_secret"
	return client.WebhookSubscriptionResponse{
		Id:                  id,
		OrganizationId:      orgID,
		TargetUrl:           "https://hooks.example.com/ingest",
		EventTypes:          []string{"email.received", "email.bounced"},
		Status:              client.WebhookSubscriptionResponseStatusActive,
		ConsecutiveFailures: 0,
		Secret:              &secret,
		CreatedAt:           now,
		UpdatedAt:           now,
	}
}

func sampleWebhookDeliveryResponse(subID openapi_types.UUID) client.WebhookDeliveryResponse {
	now := time.Date(2026, 6, 10, 12, 0, 0, 0, time.UTC)
	return client.WebhookDeliveryResponse{
		Id:             openapi_types.UUID(uuid.MustParse("33333333-3333-3333-3333-333333333333")),
		SubscriptionId: &subID,
		EventType:      "email.received",
		EventId:        openapi_types.UUID(uuid.MustParse("44444444-4444-4444-4444-444444444444")),
		Attempt:        0,
		Status:         client.WebhookDeliveryResponseStatusPending,
		CreatedAt:      now,
		NextAttemptAt:  now,
	}
}

func TestWebhooksCreate_SendsParsedEvents(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleWebhookSubscriptionResponse())

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"webhooks", "create",
		"--url", "https://hooks.example.com/ingest",
		"--events", "email.received, email.bounced",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if got := atomic.LoadInt32(&srv.hits); got != 1 {
		t.Fatalf("expected 1 request, got %d", got)
	}
	if srv.lastReq.Method != http.MethodPost || srv.lastReq.URL.Path != "/webhooks" {
		t.Fatalf("unexpected route: %s %s", srv.lastReq.Method, srv.lastReq.URL.Path)
	}

	var body client.WebhookSubscriptionCreate
	if err := json.Unmarshal(srv.lastBody, &body); err != nil {
		t.Fatalf("body parse: %v; raw=%s", err, srv.lastBody)
	}
	want := []client.WebhookSubscriptionCreateEventTypes{"email.received", "email.bounced"}
	if len(body.EventTypes) != len(want) {
		t.Fatalf("EventTypes = %v, want %v", body.EventTypes, want)
	}
	for i, e := range want {
		if body.EventTypes[i] != e {
			t.Fatalf("EventTypes[%d] = %q, want %q", i, body.EventTypes[i], e)
		}
	}
}

func TestWebhooksCreate_RejectsEmptyEvents(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleWebhookSubscriptionResponse())

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"webhooks", "create",
		"--url", "https://hooks.example.com/ingest",
		"--events", " , ",
	)
	if err == nil {
		t.Fatal("expected error on empty --events")
	}
	if !strings.Contains(err.Error(), "at least one event type") {
		t.Fatalf("error = %q, want mention of 'at least one event type'", err)
	}
	if got := atomic.LoadInt32(&srv.hits); got != 0 {
		t.Fatalf("server should not have been hit, got %d", got)
	}
}

func TestWebhooksRedeliver_HitsCorrectPath(t *testing.T) {
	subID := "11111111-1111-1111-1111-111111111111"
	deliveryID := "33333333-3333-3333-3333-333333333333"
	srv := newFakeServer(t, http.StatusOK,
		sampleWebhookDeliveryResponse(openapi_types.UUID(uuid.MustParse(subID))))

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"webhooks", "redeliver", subID, deliveryID,
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if got := atomic.LoadInt32(&srv.hits); got != 1 {
		t.Fatalf("expected 1 request, got %d", got)
	}
	wantPath := "/webhooks/" + subID + "/deliveries/" + deliveryID + "/redeliver"
	if srv.lastReq.Method != http.MethodPost || srv.lastReq.URL.Path != wantPath {
		t.Fatalf("unexpected route: %s %s, want POST %s",
			srv.lastReq.Method, srv.lastReq.URL.Path, wantPath)
	}
	if !strings.Contains(stdout, deliveryID) {
		t.Errorf("stdout missing delivery id: %q", stdout)
	}
}

func TestWebhooksList_TableByDefault(t *testing.T) {
	sub := sampleWebhookSubscriptionResponse()
	srv := newFakeServer(t, http.StatusOK, client.WebhookSubscriptionListResponse{
		Items: []client.WebhookSubscriptionResponse{sub},
	})

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"webhooks", "list",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if strings.HasPrefix(strings.TrimSpace(stdout), "{") {
		t.Fatalf("default output looks like raw JSON, want a table: %q", stdout)
	}
	for _, col := range []string{"ID", "URL", "EVENTS", "STATUS", "FAILURES"} {
		if !strings.Contains(stdout, col) {
			t.Errorf("stdout missing column header %q: %q", col, stdout)
		}
	}
	if !strings.Contains(stdout, sub.TargetUrl) {
		t.Errorf("stdout missing subscription URL %q: %q", sub.TargetUrl, stdout)
	}
}

func TestWebhooksList_JSONFlag(t *testing.T) {
	sub := sampleWebhookSubscriptionResponse()
	srv := newFakeServer(t, http.StatusOK, client.WebhookSubscriptionListResponse{
		Items: []client.WebhookSubscriptionResponse{sub},
	})

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"webhooks", "list", "--json",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	var body client.WebhookSubscriptionListResponse
	if err := json.Unmarshal([]byte(stdout), &body); err != nil {
		t.Fatalf("--json output is not valid JSON: %v; raw=%q", err, stdout)
	}
	if len(body.Items) != 1 || body.Items[0].Id != sub.Id {
		t.Fatalf("decoded JSON missing subscription %s: %+v", sub.Id, body)
	}
}
