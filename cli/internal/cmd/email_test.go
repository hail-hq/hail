package cmd

import (
	"encoding/json"
	"net/http"
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
		EmailDomainId:     sdID,
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

func TestEmailSend_RequiresBody(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleEmailResponse())

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "send", "--to", "x@example.com", "--subject", "hi",
	)
	if err == nil {
		t.Fatal("expected error when no body supplied")
	}
	if !strings.Contains(err.Error(), "body") {
		t.Errorf("unexpected error: %v", err)
	}
	if got := atomic.LoadInt32(&srv.hits); got != 0 {
		t.Fatalf("server should not have been called, got %d hits", got)
	}
}

func TestEmailSend_RequiresSubject(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleEmailResponse())

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "send", "--to", "x@example.com", "--body", "hi",
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

func TestEmailSend_APIError(t *testing.T) {
	srv := newFakeServer(t, http.StatusBadGateway, map[string]string{"detail": "email send failed"})

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "send",
		"--to", "x@example.com",
		"--subject", "hi",
		"--body", "hello",
	)
	if err == nil {
		t.Fatal("expected error on 502")
	}
	if !strings.Contains(err.Error(), "502") {
		t.Errorf("error should reference status code: %v", err)
	}
}
