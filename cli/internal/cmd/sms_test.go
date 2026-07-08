package cmd

import (
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

// sampleSms returns an SmsResponse populated with realistic data, mirroring
// sampleCall's shape.
func sampleSms(idStr, to string, status client.SmsResponseStatus) client.SmsResponse {
	id := openapi_types.UUID(uuid.MustParse(idStr))
	orgID := openapi_types.UUID(uuid.MustParse("22222222-2222-2222-2222-222222222222"))
	now := time.Date(2026, 4, 22, 12, 0, 0, 0, time.UTC)
	return client.SmsResponse{
		Id:             id,
		OrganizationId: orgID,
		FromE164:       "+14155559999",
		ToE164:         to,
		Body:           "hello",
		Direction:      client.SmsResponseDirectionOutbound,
		Status:         status,
		SegmentCount:   1,
		RequestedAt:    now,
	}
}

func TestSmsSubcommand_HappyPath(t *testing.T) {
	resp := sampleSms("11111111-1111-1111-1111-111111111111", "+14155551234", client.SmsResponseStatusSent)
	srv := newFakeServer(t, http.StatusCreated, resp)

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"sms", "+14155551234", "--body", "hello", "--recipient-consent",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if got := atomic.LoadInt32(&srv.hits); got != 1 {
		t.Fatalf("expected 1 request, got %d", got)
	}
	if srv.lastReq.Method != http.MethodPost || srv.lastReq.URL.Path != "/sms" {
		t.Fatalf("unexpected route: %s %s", srv.lastReq.Method, srv.lastReq.URL.Path)
	}
	if h := srv.lastReq.Header.Get("Authorization"); h != "Bearer sk_test" {
		t.Fatalf("Authorization header = %q", h)
	}
	if h := srv.lastReq.Header.Get("Idempotency-Key"); h == "" {
		t.Fatal("Idempotency-Key header missing")
	}

	var body client.SmsCreate
	if err := json.Unmarshal(srv.lastBody, &body); err != nil {
		t.Fatalf("body parse: %v; raw=%s", err, srv.lastBody)
	}
	if body.To != "+14155551234" {
		t.Fatalf("To = %q", body.To)
	}
	if body.Body != "hello" {
		t.Fatalf("Body = %q", body.Body)
	}
	if !body.RecipientConsent {
		t.Fatal("RecipientConsent should be true")
	}

	if !strings.Contains(stdout, "SMS sent") {
		t.Errorf("expected success output, got: %q", stdout)
	}
	if !strings.Contains(stdout, resp.Id.String()) {
		t.Errorf("stdout missing sms id: %q", stdout)
	}
	if !strings.Contains(stdout, "sent") {
		t.Errorf("stdout missing status: %q", stdout)
	}
}

func TestSmsSubcommand_CarrierRejection_DoesNotClaimSuccess(t *testing.T) {
	resp := sampleSms("11111111-1111-1111-1111-111111111111", "+14155551234", client.SmsResponseStatusFailed)
	resp.ErrorCode = strPtr("30006")
	srv := newFakeServer(t, http.StatusCreated, resp)

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"sms", "+14155551234", "--body", "hello", "--recipient-consent",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if strings.Contains(stdout, "✓") || strings.Contains(stdout, "SMS sent") {
		t.Errorf("expected no success claim for a failed send, got: %q", stdout)
	}
	if !strings.Contains(stdout, "failed") {
		t.Errorf("stdout missing failed status: %q", stdout)
	}
	if !strings.Contains(stdout, "30006") {
		t.Errorf("stdout missing error code: %q", stdout)
	}
}

func TestSmsSubcommand_FromAndMessageTypeFlow(t *testing.T) {
	resp := sampleSms("11111111-1111-1111-1111-111111111111", "+14155551234", client.SmsResponseStatusSent)
	srv := newFakeServer(t, http.StatusCreated, resp)

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"sms", "+14155551234",
		"--body", "hello",
		"--from", "+14155550000",
		"--recipient-consent",
		"--consent-source", "signup_form",
		"--message-type", "marketing",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	var body map[string]any
	if err := json.Unmarshal(srv.lastBody, &body); err != nil {
		t.Fatalf("bad request body: %v", err)
	}
	if body["from"] != "+14155550000" {
		t.Errorf("from = %v, want +14155550000", body["from"])
	}
	if body["consent_source"] != "signup_form" {
		t.Errorf("consent_source = %v, want signup_form", body["consent_source"])
	}
	if body["message_type"] != "marketing" {
		t.Errorf("message_type = %v, want marketing", body["message_type"])
	}
}

func TestSmsSubcommand_PropagatesIdempotencyKey(t *testing.T) {
	t.Run("explicit", func(t *testing.T) {
		resp := sampleSms("11111111-1111-1111-1111-111111111111", "+14155551234", client.SmsResponseStatusSent)
		srv := newFakeServer(t, http.StatusCreated, resp)
		_, _, err := runRoot(t,
			map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
			"sms", "+14155551234", "--body", "hi", "--idempotency-key", "deadbeef-1234", "--recipient-consent",
		)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if h := srv.lastReq.Header.Get("Idempotency-Key"); h != "deadbeef-1234" {
			t.Errorf("Idempotency-Key = %q, want deadbeef-1234", h)
		}
	})
	t.Run("auto-uuid", func(t *testing.T) {
		resp := sampleSms("11111111-1111-1111-1111-111111111111", "+14155551234", client.SmsResponseStatusSent)
		srv := newFakeServer(t, http.StatusCreated, resp)
		_, _, err := runRoot(t,
			map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
			"sms", "+14155551234", "--body", "hi", "--recipient-consent",
		)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		h := srv.lastReq.Header.Get("Idempotency-Key")
		if _, err := uuid.Parse(h); err != nil {
			t.Errorf("Idempotency-Key %q not a UUID: %v", h, err)
		}
	})
}

func TestSmsSubcommand_HandlesAPIError(t *testing.T) {
	errBody := client.HTTPValidationError{
		Detail: &[]client.ValidationError{{
			Loc:  []client.ValidationError_Loc_Item{},
			Msg:  "to: must be E.164",
			Type: "value_error",
		}},
	}
	srv := newFakeServer(t, http.StatusUnprocessableEntity, errBody)

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"sms", "+14155551234", "--body", "hi", "--recipient-consent",
	)
	if err == nil {
		t.Fatal("expected error from server, got nil")
	}
	if !strings.Contains(err.Error(), "must be E.164") {
		t.Errorf("error = %v; expected detail message", err)
	}
}

func TestSmsSubcommand_JSONOutput(t *testing.T) {
	resp := sampleSms("11111111-1111-1111-1111-111111111111", "+14155551234", client.SmsResponseStatusSent)
	srv := newFakeServer(t, http.StatusCreated, resp)

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"--json", "sms", "+14155551234", "--body", "hi", "--recipient-consent",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var got client.SmsResponse
	if err := json.Unmarshal([]byte(stdout), &got); err != nil {
		t.Fatalf("stdout is not valid JSON: %v\n%s", err, stdout)
	}
	if got.Id != resp.Id {
		t.Errorf("Id = %v", got.Id)
	}
}

// TestSmsSubcommand_RecipientConsent_OmittedFailsBeforeNetwork pins the
// MarkFlagRequired("recipient-consent") gap: omitting the flag entirely
// fails in PreRunE (requireMarkedFlags), before any HTTP request is made.
func TestSmsSubcommand_RecipientConsent_OmittedFailsBeforeNetwork(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleSms("11111111-1111-1111-1111-111111111111", "+14155551234", client.SmsResponseStatusSent))

	_, stderr, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"sms", "+14155551234", "--body", "hi",
	)
	if !strings.Contains(err.Error(), "invalid inputs") {
		t.Fatalf("want errInvalidInputs, got %v", err)
	}
	if !strings.Contains(stderr, `required flag(s) "recipient-consent" not set`) {
		t.Errorf("stderr missing reason: %q", stderr)
	}
	if hits := atomic.LoadInt32(&srv.hits); hits != 0 {
		t.Errorf("expected 0 HTTP calls, got %d", hits)
	}
}

// TestSmsSubcommand_RecipientConsent_FalseFailsBeforeNetwork pins the gap
// MarkFlagRequired alone cannot close: `--recipient-consent=false` sets
// pflag.Changed, satisfying ValidateRequiredFlags, but the API requires the
// value to actually be true. requireTrueFlag (called first in runSms)
// catches this and fails before any HTTP request is made.
func TestSmsSubcommand_RecipientConsent_FalseFailsBeforeNetwork(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleSms("11111111-1111-1111-1111-111111111111", "+14155551234", client.SmsResponseStatusSent))

	_, stderr, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"sms", "+14155551234", "--body", "hi", "--recipient-consent=false",
	)
	if err == nil {
		t.Fatal("expected error")
	}
	if !strings.Contains(stderr, "--recipient-consent must be true") {
		t.Errorf("stderr missing reason: %q", stderr)
	}
	if hits := atomic.LoadInt32(&srv.hits); hits != 0 {
		t.Errorf("expected 0 HTTP calls, got %d", hits)
	}
}

func TestSmsSubcommand_MissingBody_FailsBeforeNetwork(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleSms("11111111-1111-1111-1111-111111111111", "+14155551234", client.SmsResponseStatusSent))

	_, stderr, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"sms", "+14155551234", "--recipient-consent",
	)
	if err == nil {
		t.Fatal("expected error")
	}
	if !strings.Contains(stderr, `required flag(s) "body" not set`) {
		t.Errorf("stderr missing reason: %q", stderr)
	}
	if hits := atomic.LoadInt32(&srv.hits); hits != 0 {
		t.Errorf("expected 0 HTTP calls, got %d", hits)
	}
}

func TestSmsSubcommand_MarketingRequiresConsentSource(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleSms("11111111-1111-1111-1111-111111111111", "+14155551234", client.SmsResponseStatusSent))

	_, stderr, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"sms", "+14155551234", "--body", "hi", "--recipient-consent", "--message-type", "marketing",
	)
	if err == nil {
		t.Fatal("expected error")
	}
	if !strings.Contains(stderr, "--consent-source") {
		t.Errorf("stderr missing reason: %q", stderr)
	}
	if hits := atomic.LoadInt32(&srv.hits); hits != 0 {
		t.Errorf("expected 0 HTTP calls, got %d", hits)
	}
}

func TestSmsStatus_HappyPath(t *testing.T) {
	resp := sampleSms("11111111-1111-1111-1111-111111111111", "+14155551234", client.SmsResponseStatusDelivered)
	sentAt := time.Date(2026, 4, 22, 12, 0, 30, 0, time.UTC)
	resp.SentAt = &sentAt
	sid := "SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
	resp.ProviderMessageSid = &sid

	srv := newFakeServer(t, http.StatusOK, resp)

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"sms", "status", resp.Id.String(),
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(stdout, resp.Id.String()) {
		t.Errorf("missing id in stdout: %q", stdout)
	}
	if !strings.Contains(stdout, "delivered") {
		t.Errorf("missing status in stdout: %q", stdout)
	}
	if !strings.Contains(stdout, "Provider:  "+sid) {
		t.Errorf("missing provider sid in stdout: %q", stdout)
	}
	if srv.lastReq.URL.Path != "/sms/"+resp.Id.String() {
		t.Errorf("path = %s", srv.lastReq.URL.Path)
	}
	if srv.lastReq.Method != http.MethodGet {
		t.Errorf("method = %s", srv.lastReq.Method)
	}
}

func TestSmsStatus_NotFound(t *testing.T) {
	srv := newFakeServer(t, http.StatusNotFound, map[string]string{"detail": "sms not found"})

	id := uuid.NewString()
	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"sms", "status", id,
	)
	if err == nil {
		t.Fatal("expected error, got nil")
	}
	if !strings.Contains(err.Error(), "not found") {
		t.Errorf("error = %v", err)
	}
}

func TestSmsStatus_RejectsBadShape(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, sampleSms("11111111-1111-1111-1111-111111111111", "+14155551234", client.SmsResponseStatusSent))

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"sms", "status", "not-a-uuid",
	)
	if err == nil {
		t.Fatal("expected error")
	}
	if !strings.Contains(err.Error(), "invalid sms id") {
		t.Errorf("error = %v", err)
	}
	if hits := atomic.LoadInt32(&srv.hits); hits != 0 {
		t.Errorf("expected 0 HTTP calls for malformed input, got %d", hits)
	}
}

func TestSmsList_RendersTable(t *testing.T) {
	items := []client.SmsResponse{
		sampleSms("11111111-1111-1111-1111-111111111111", "+15551110001", client.SmsResponseStatusDelivered),
		sampleSms("22222222-2222-2222-2222-222222222221", "+15551110002", client.SmsResponseStatusQueued),
		sampleSms("33333333-3333-3333-3333-333333333331", "+15551110003", client.SmsResponseStatusFailed),
	}
	srv := newFakeServer(t, http.StatusOK, client.SmsListResponse{Items: items})

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"sms", "list",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(stdout, "ID") || !strings.Contains(stdout, "TO") || !strings.Contains(stdout, "STATUS") || !strings.Contains(stdout, "REQUESTED") {
		t.Errorf("missing header columns in stdout:\n%s", stdout)
	}
	for _, to := range []string{"+15551110001", "+15551110002", "+15551110003"} {
		if !strings.Contains(stdout, to) {
			t.Errorf("missing %s in stdout:\n%s", to, stdout)
		}
	}
	if !strings.Contains(stdout, "delivered") || !strings.Contains(stdout, "queued") || !strings.Contains(stdout, "failed") {
		t.Errorf("missing statuses in stdout:\n%s", stdout)
	}
}

func TestSmsList_Empty(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, client.SmsListResponse{Items: nil})

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"sms", "list",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(stdout, "(no sms)") {
		t.Errorf("expected empty-state message, got: %q", stdout)
	}
}

func TestSmsList_StatusFilter(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, client.SmsListResponse{Items: nil})

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"sms", "list", "--status", "delivered",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got := srv.lastReq.URL.Query().Get("status"); got != "delivered" {
		t.Errorf("?status = %q, want delivered", got)
	}
}
