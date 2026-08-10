package cmd

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/google/uuid"
	openapi_types "github.com/oapi-codegen/runtime/types"
	"os"

	"github.com/hail-hq/hail/cli/internal/client"
)

func sampleEmailResponse() client.EmailResponse {
	id := openapi_types.UUID(uuid.MustParse("11111111-1111-1111-1111-111111111111"))
	orgID := openapi_types.UUID(uuid.MustParse("22222222-2222-2222-2222-222222222222"))
	sdID := openapi_types.UUID(uuid.MustParse("33333333-3333-3333-3333-333333333333"))
	now := time.Date(2026, 5, 17, 12, 0, 0, 0, time.UTC)
	msgID := "0102018f-ses-test"
	bodyText := "hello"
	return client.EmailResponse{
		Id:                id,
		OrganizationId:    orgID,
		EmailDomainId:     &sdID,
		FromAddress:       "alice+acme@mail.hail.so",
		ToAddresses:       []string{"x@example.com"},
		Subject:           "hi",
		BodyText:          &bodyText,
		Status:            client.EmailResponseStatusSent,
		ProviderMessageId: &msgID,
		RequestedAt:       now,
		SentAt:            &now,
	}
}

func TestEmailSend_HappyPath(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleEmailResponse())

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "send",
		"--to", "x@example.com",
		"--subject", "hi",
		"--body", "hello",
		"--recipient-consent",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if got := atomic.LoadInt32(&srv.hits); got != 1 {
		t.Fatalf("expected 1 request, got %d", got)
	}
	if srv.lastReq.Method != http.MethodPost || srv.lastReq.URL.Path != "/emails" {
		t.Fatalf("unexpected route: %s %s", srv.lastReq.Method, srv.lastReq.URL.Path)
	}
	if h := srv.lastReq.Header.Get("Authorization"); h != "Bearer sk_test" {
		t.Fatalf("Authorization header = %q", h)
	}
	if h := srv.lastReq.Header.Get("Idempotency-Key"); h == "" {
		t.Fatal("Idempotency-Key header missing")
	}

	var body client.EmailCreate
	if err := json.Unmarshal(srv.lastBody, &body); err != nil {
		t.Fatalf("body parse: %v; raw=%s", err, srv.lastBody)
	}
	if len(body.To) != 1 || body.To[0] != "x@example.com" {
		t.Fatalf("To = %v", body.To)
	}
	if body.Subject != "hi" {
		t.Fatalf("Subject = %q", body.Subject)
	}
	if body.BodyText == nil || *body.BodyText != "hello" {
		t.Fatalf("BodyText = %v", body.BodyText)
	}
	if body.BodyHtml != nil {
		t.Fatalf("BodyHtml should be nil, got %v", body.BodyHtml)
	}

	if !strings.Contains(stdout, "alice+acme@mail.hail.so") {
		t.Errorf("stdout missing from-address: %q", stdout)
	}
	if !strings.Contains(stdout, "sent") {
		t.Errorf("stdout missing status: %q", stdout)
	}
}

func TestEmailSend_FromName(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleEmailResponse())

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "send",
		"--to", "x@example.com",
		"--subject", "hi",
		"--body", "hello",
		"--from-name", "Acme Billing",
		"--recipient-consent",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	var body client.EmailCreate
	if err := json.Unmarshal(srv.lastBody, &body); err != nil {
		t.Fatalf("body parse: %v; raw=%s", err, srv.lastBody)
	}
	if body.FromName == nil || *body.FromName != "Acme Billing" {
		t.Fatalf("FromName = %v", body.FromName)
	}
}

func TestEmailSend_RequiresBody(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleEmailResponse())

	_, stderr, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "send", "--to", "x@example.com", "--subject", "hi", "--recipient-consent",
	)
	if !errors.Is(err, errInvalidInputs) {
		t.Fatalf("want errInvalidInputs, got %v", err)
	}
	if !strings.Contains(stderr, "--body or --body-html") {
		t.Errorf("stderr missing body mention: %q", stderr)
	}
	if got := atomic.LoadInt32(&srv.hits); got != 0 {
		t.Fatalf("server should not have been called, got %d hits", got)
	}
}

func TestEmailSend_RequiresSubject(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleEmailResponse())

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "send", "--to", "x@example.com", "--body", "hi", "--recipient-consent",
	)
	if err == nil {
		t.Fatal("expected error when --subject omitted")
	}
}

func TestEmailSend_MultipleRecipients(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleEmailResponse())

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "send",
		"--to", "a@example.com,b@example.com",
		"--cc", "c@example.com",
		"--subject", "hi",
		"--body", "hello",
		"--recipient-consent",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	var body client.EmailCreate
	if err := json.Unmarshal(srv.lastBody, &body); err != nil {
		t.Fatalf("body parse: %v; raw=%s", err, srv.lastBody)
	}
	if len(body.To) != 2 || body.To[0] != "a@example.com" || body.To[1] != "b@example.com" {
		t.Fatalf("To = %v", body.To)
	}
	if body.Cc == nil || len(*body.Cc) != 1 || (*body.Cc)[0] != "c@example.com" {
		t.Fatalf("Cc = %v", body.Cc)
	}
}

func TestEmailSend_BodyFile(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleEmailResponse())

	dir := t.TempDir()
	bodyPath := filepath.Join(dir, "body.txt")
	if err := os.WriteFile(bodyPath, []byte("file body content"), 0o644); err != nil {
		t.Fatalf("write body file: %v", err)
	}

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "send",
		"--to", "x@example.com",
		"--subject", "hi",
		"--body-file", bodyPath,
		"--recipient-consent",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	var body client.EmailCreate
	if err := json.Unmarshal(srv.lastBody, &body); err != nil {
		t.Fatalf("body parse: %v; raw=%s", err, srv.lastBody)
	}
	if body.BodyText == nil || *body.BodyText != "file body content" {
		t.Fatalf("BodyText = %v", body.BodyText)
	}
}

func TestEmailSend_MissingSubject_PrintsHelpAndFails(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, map[string]any{})
	_, stderr, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "send", "--to", "a@b.com", "--body", "hi", "--recipient-consent",
	)
	if !errors.Is(err, errInvalidInputs) {
		t.Fatalf("want errInvalidInputs, got %v", err)
	}
	if !strings.Contains(stderr, `required flag(s) "subject" not set`) {
		t.Fatalf("missing reason line: %q", stderr)
	}
	if !strings.Contains(stderr, "hail email send") {
		t.Fatalf("expected Help() output in stderr: %q", stderr)
	}
}

func TestEmailSend_APIError(t *testing.T) {
	srv := newFakeServer(t, http.StatusBadGateway, map[string]string{"detail": "email send failed"})

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "send",
		"--to", "x@example.com",
		"--subject", "hi",
		"--body", "hello",
		"--recipient-consent",
	)
	if err == nil {
		t.Fatal("expected error on 502")
	}
	if !strings.Contains(err.Error(), "502") {
		t.Errorf("error should reference status code: %v", err)
	}
}

// TestEmailSend_OversizeAttachmentRejection guards against a real bug: the
// server's attachment size-cap rejection returns a plain string `detail`
// ({"detail": "..."}), not FastAPI's list-shaped validation-error detail
// that the generated WithResponse parser expects for every 422. Using that
// generated parser directly made the real "too large" message get replaced
// by a raw Go json.Unmarshal error. This must surface the actual server
// message instead.
func TestEmailSend_OversizeAttachmentRejection(t *testing.T) {
	srv := newFakeServer(t, http.StatusUnprocessableEntity, map[string]string{
		"detail": "attachment(s) too large — host the file externally and include a link in the body instead",
	})

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "send",
		"--to", "x@example.com",
		"--subject", "hi",
		"--body", "hello",
		"--recipient-consent",
	)
	if err == nil {
		t.Fatal("expected error on 422")
	}
	if strings.Contains(err.Error(), "cannot unmarshal") {
		t.Fatalf("leaked raw json unmarshal error instead of the server message: %v", err)
	}
	if !strings.Contains(err.Error(), "too large") {
		t.Fatalf("expected the real oversize message, got: %v", err)
	}
}

func TestEmailSendSubcommand_SendsConsentFlags(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleEmailResponse())

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "send",
		"--to", "a@example.com",
		"--subject", "hi",
		"--body", "hello",
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
	if body["recipient_consent"] != true {
		t.Errorf("recipient_consent = %v, want true", body["recipient_consent"])
	}
	if body["consent_source"] != "signup_form" {
		t.Errorf("consent_source = %v, want signup_form", body["consent_source"])
	}
}

// TestEmailSendSubcommand_RecipientConsent_OmittedFailsBeforeNetwork pins
// the MarkFlagRequired("recipient-consent") gap: omitting the flag entirely
// fails in PreRunE (requireMarkedFlags), before any HTTP request is made.
func TestEmailSendSubcommand_RecipientConsent_OmittedFailsBeforeNetwork(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleEmailResponse())

	_, stderr, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "send",
		"--to", "a@example.com",
		"--subject", "hi",
		"--body", "hello",
	)
	if !errors.Is(err, errInvalidInputs) {
		t.Fatalf("want errInvalidInputs, got %v", err)
	}
	if !strings.Contains(stderr, `required flag(s) "recipient-consent" not set`) {
		t.Errorf("stderr missing reason: %q", stderr)
	}
	if hits := atomic.LoadInt32(&srv.hits); hits != 0 {
		t.Errorf("expected 0 HTTP calls, got %d", hits)
	}
}

// TestEmailSendSubcommand_RecipientConsent_FalseFailsBeforeNetwork pins the
// gap MarkFlagRequired alone cannot close: `--recipient-consent=false` sets
// pflag.Changed, satisfying ValidateRequiredFlags, but the API requires the
// value to actually be true. requireTrueFlag (called first in
// runEmailSend) catches this and fails before any HTTP request is made.
func TestEmailSendSubcommand_RecipientConsent_FalseFailsBeforeNetwork(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleEmailResponse())

	_, stderr, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "send",
		"--to", "a@example.com",
		"--subject", "hi",
		"--body", "hello",
		"--recipient-consent=false",
	)
	if !errors.Is(err, errInvalidInputs) {
		t.Fatalf("want errInvalidInputs, got %v", err)
	}
	if !strings.Contains(stderr, "--recipient-consent must be true") {
		t.Errorf("stderr missing reason: %q", stderr)
	}
	if hits := atomic.LoadInt32(&srv.hits); hits != 0 {
		t.Errorf("expected 0 HTTP calls, got %d", hits)
	}
}

// TestEmailSend_AttachFlag exercises the --attach upload-then-send path
// against a two-route fake server: --attach must upload the file to
// POST /email-attachments first, then thread the returned id into
// EmailCreate.attachment_ids on the POST /emails call.
func TestEmailSend_AttachFlag(t *testing.T) {
	dir := t.TempDir()
	filePath := filepath.Join(dir, "invoice.pdf")
	if err := os.WriteFile(filePath, []byte("pdf bytes"), 0o644); err != nil {
		t.Fatalf("write attach file: %v", err)
	}

	mux := http.NewServeMux()
	var sendBody []byte
	mux.HandleFunc("/email-attachments", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(map[string]any{
			"id":           "11111111-1111-1111-1111-111111111111",
			"filename":     "invoice.pdf",
			"content_type": "application/octet-stream",
			"size_bytes":   9,
		})
	})
	mux.HandleFunc("/emails", func(w http.ResponseWriter, r *http.Request) {
		sendBody, _ = io.ReadAll(r.Body)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		email := sampleEmailResponse()
		_ = json.NewEncoder(w).Encode(email)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "send",
		"--to", "x@example.com",
		"--subject", "hi",
		"--body", "hello",
		"--recipient-consent",
		"--attach", filePath,
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	var body map[string]any
	if err := json.Unmarshal(sendBody, &body); err != nil {
		t.Fatalf("bad send body: %v", err)
	}
	ids, ok := body["attachment_ids"].([]any)
	if !ok || len(ids) != 1 || ids[0] != "11111111-1111-1111-1111-111111111111" {
		t.Fatalf("attachment_ids = %v", body["attachment_ids"])
	}
}
