package cmd

import (
	"encoding/json"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/google/uuid"
	openapi_types "github.com/oapi-codegen/runtime/types"

	"github.com/hail-hq/hail/cli/internal/client"
)

func samplePhoneNumber(idStr, e164 string, caps []string, msgSid *string) client.PhoneNumberResponse {
	id := openapi_types.UUID(uuid.MustParse(idStr))
	return client.PhoneNumberResponse{
		Id:                  id,
		E164:                e164,
		CountryCode:         "US",
		NumberType:          "local",
		Capabilities:        caps,
		ProvisioningState:   "active",
		IsDedicated:         true,
		MessagingServiceSid: msgSid,
	}
}

func TestNumberAcquire_HappyPath(t *testing.T) {
	resp := samplePhoneNumber("11111111-1111-1111-1111-111111111111", "+14155551234", []string{"voice", "sms"}, nil)
	srv := newFakeServer(t, http.StatusCreated, resp)

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"numbers", "acquire", "--country", "US",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if got := atomic.LoadInt32(&srv.hits); got != 1 {
		t.Fatalf("expected 1 request, got %d", got)
	}
	if srv.lastReq.Method != http.MethodPost || srv.lastReq.URL.Path != "/numbers" {
		t.Fatalf("unexpected route: %s %s", srv.lastReq.Method, srv.lastReq.URL.Path)
	}
	if h := srv.lastReq.Header.Get("Idempotency-Key"); h == "" {
		t.Fatal("Idempotency-Key header missing")
	}

	var body client.NumberAcquireRequest
	if err := json.Unmarshal(srv.lastBody, &body); err != nil {
		t.Fatalf("body parse: %v; raw=%s", err, srv.lastBody)
	}
	if body.CountryCode != "US" {
		t.Fatalf("CountryCode = %q", body.CountryCode)
	}
	if body.NumberType == nil || *body.NumberType != "local" {
		t.Fatalf("NumberType = %v", body.NumberType)
	}
	if !strings.Contains(stdout, "+14155551234") {
		t.Errorf("stdout missing number: %q", stdout)
	}
	if !strings.Contains(stdout, "voice, sms") {
		t.Errorf("stdout missing capabilities: %q", stdout)
	}
}

func TestNumberAcquire_TollFreeType(t *testing.T) {
	resp := samplePhoneNumber("11111111-1111-1111-1111-111111111111", "+18005551234", []string{"voice", "sms"}, nil)
	srv := newFakeServer(t, http.StatusCreated, resp)

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"numbers", "acquire", "--country", "US", "--type", "toll_free",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var body client.NumberAcquireRequest
	if err := json.Unmarshal(srv.lastBody, &body); err != nil {
		t.Fatalf("body parse: %v; raw=%s", err, srv.lastBody)
	}
	if body.NumberType == nil || *body.NumberType != "toll_free" {
		t.Fatalf("NumberType = %v", body.NumberType)
	}
}

func TestNumberAcquire_NationalType(t *testing.T) {
	resp := samplePhoneNumber("11111111-1111-1111-1111-111111111111", "+552135551234", []string{"voice", "sms"}, nil)
	srv := newFakeServer(t, http.StatusCreated, resp)

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"numbers", "acquire", "--country", "BR", "--type", "national",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var body client.NumberAcquireRequest
	if err := json.Unmarshal(srv.lastBody, &body); err != nil {
		t.Fatalf("body parse: %v; raw=%s", err, srv.lastBody)
	}
	if body.NumberType == nil || *body.NumberType != "national" {
		t.Fatalf("NumberType = %v", body.NumberType)
	}
}

func TestNumberAcquire_MissingCountryFailsBeforeNetwork(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, samplePhoneNumber("11111111-1111-1111-1111-111111111111", "+14155551234", []string{"voice"}, nil))

	_, stderr, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"numbers", "acquire",
	)
	if err == nil {
		t.Fatal("expected error on missing --country")
	}
	if !strings.Contains(stderr, `required flag(s) "country" not set`) {
		t.Errorf("stderr missing reason: %q", stderr)
	}
	if hits := atomic.LoadInt32(&srv.hits); hits != 0 {
		t.Errorf("expected 0 HTTP calls, got %d", hits)
	}
}

func TestNumberAcquire_RejectsBadType(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, samplePhoneNumber("11111111-1111-1111-1111-111111111111", "+14155551234", []string{"voice"}, nil))

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"numbers", "acquire", "--country", "US", "--type", "satellite",
	)
	if err == nil {
		t.Fatal("expected error on invalid --type")
	}
	if hits := atomic.LoadInt32(&srv.hits); hits != 0 {
		t.Errorf("server should not have been hit, got %d", hits)
	}
}

func TestNumberList_RendersTable(t *testing.T) {
	items := []client.PhoneNumberResponse{
		samplePhoneNumber("11111111-1111-1111-1111-111111111111", "+15551110001", []string{"voice", "sms"}, nil),
		samplePhoneNumber("22222222-2222-2222-2222-222222222221", "+15551110002", []string{"voice"}, nil),
	}
	srv := newFakeServer(t, http.StatusOK, client.PhoneNumberListResponse{Items: items})

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"numbers", "list",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if srv.lastReq.URL.Path != "/numbers" {
		t.Fatalf("unexpected path: %s", srv.lastReq.URL.Path)
	}
	for _, want := range []string{"ID", "E164", "TYPE", "CAPABILITIES", "STATE", "+15551110001", "+15551110002"} {
		if !strings.Contains(stdout, want) {
			t.Errorf("missing %q in stdout:\n%s", want, stdout)
		}
	}
}

func TestNumberList_Empty(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, client.PhoneNumberListResponse{Items: nil})

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"numbers", "list",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(stdout, "(no numbers)") {
		t.Errorf("expected empty-state message, got: %q", stdout)
	}
}

func TestNumberGet_HappyPath(t *testing.T) {
	sid := "MG0123456789abcdef"
	resp := samplePhoneNumber("11111111-1111-1111-1111-111111111111", "+14155551234", []string{"voice", "sms"}, &sid)
	srv := newFakeServer(t, http.StatusOK, resp)

	// Full UUID short-circuits prefix resolution — no list roundtrip.
	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"numbers", "get", resp.Id.String(),
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if srv.lastReq.Method != http.MethodGet || srv.lastReq.URL.Path != "/numbers/"+resp.Id.String() {
		t.Fatalf("unexpected route: %s %s", srv.lastReq.Method, srv.lastReq.URL.Path)
	}
	if !strings.Contains(stdout, "+14155551234") {
		t.Errorf("stdout missing number: %q", stdout)
	}
	if !strings.Contains(stdout, sid) {
		t.Errorf("stdout missing messaging service sid: %q", stdout)
	}
}

func TestNumberGet_RejectsBadShape(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, samplePhoneNumber("11111111-1111-1111-1111-111111111111", "+14155551234", []string{"voice"}, nil))

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"numbers", "get", "xyz",
	)
	if err == nil {
		t.Fatal("expected error on malformed id")
	}
	if hits := atomic.LoadInt32(&srv.hits); hits != 0 {
		t.Errorf("expected 0 HTTP calls for malformed input, got %d", hits)
	}
}

func TestNumberEnableSms_HappyPath(t *testing.T) {
	sid := "MG0123456789abcdef"
	resp := samplePhoneNumber("11111111-1111-1111-1111-111111111111", "+14155551234", []string{"voice", "sms"}, &sid)
	srv := newFakeServer(t, http.StatusOK, resp)

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"numbers", "enable-sms", resp.Id.String(),
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if srv.lastReq.Method != http.MethodPost || srv.lastReq.URL.Path != "/numbers/"+resp.Id.String()+"/enable-sms" {
		t.Fatalf("unexpected route: %s %s", srv.lastReq.Method, srv.lastReq.URL.Path)
	}
	if !strings.Contains(stdout, sid) {
		t.Errorf("stdout missing messaging service sid: %q", stdout)
	}
}

func TestNumberGet_JSONOutput(t *testing.T) {
	resp := samplePhoneNumber("11111111-1111-1111-1111-111111111111", "+14155551234", []string{"voice", "sms"}, nil)
	srv := newFakeServer(t, http.StatusOK, resp)

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"--json", "numbers", "get", resp.Id.String(),
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var got client.PhoneNumberResponse
	if err := json.Unmarshal([]byte(stdout), &got); err != nil {
		t.Fatalf("stdout is not valid JSON: %v\n%s", err, stdout)
	}
	if got.Id != resp.Id {
		t.Errorf("Id = %v", got.Id)
	}
}
