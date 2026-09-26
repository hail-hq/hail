package cmd

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

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

// acquireServer answers POST /v1/numbers/quotes with the given offers and
// POST /v1/numbers with the given phone number, recording each request body.
type acquireServer struct {
	*httptest.Server
	quoteBody []byte
	buyBody   []byte
	buyKey    string
	quotes    int32
	buys      int32
}

func newAcquireServer(t *testing.T, offers []client.CarrierOffer, bought client.PhoneNumberResponse) *acquireServer {
	t.Helper()
	as := &acquireServer{}
	as.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/v1/numbers/quotes":
			atomic.AddInt32(&as.quotes, 1)
			as.quoteBody = body
			w.WriteHeader(http.StatusOK)
			_ = json.NewEncoder(w).Encode(client.NumberQuotesResponse{
				Offers:               offers,
				UnavailableProviders: []string{},
				ExpiresAt:            time.Now().Add(10 * time.Minute),
			})
		case "/v1/numbers":
			atomic.AddInt32(&as.buys, 1)
			as.buyBody = body
			as.buyKey = r.Header.Get("Idempotency-Key")
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(bought)
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	t.Cleanup(as.Close)
	return as
}

func sampleOffer(quoteID string, monthly, setup int, readiness string) client.CarrierOffer {
	id := openapi_types.UUID(uuid.MustParse(quoteID))
	return client.CarrierOffer{
		Capabilities: []string{"voice", "sms"},
		CountryCode:  "US",
		E164:         "+14155550000",
		MonthlyCents: monthly,
		NumberType:   "local",
		Provider:     "twilio",
		QuoteId:      &id,
		Readiness:    client.CarrierOfferReadiness(readiness),
		SetupCents:   setup,
	}
}

const (
	quoteExpensive = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
	quoteCheap     = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
	quoteNotReady  = "cccccccc-cccc-cccc-cccc-cccccccccccc"
)

func TestNumberAcquire_BuysTheCheapestReadyOffer(t *testing.T) {
	bought := samplePhoneNumber("11111111-1111-1111-1111-111111111111", "+14155551234", []string{"voice", "sms"}, nil)
	srv := newAcquireServer(t, []client.CarrierOffer{
		sampleOffer(quoteNotReady, 10, 0, "verification_required"),
		sampleOffer(quoteExpensive, 200, 0, "ready"),
		sampleOffer(quoteCheap, 100, 50, "ready"),
	}, bought)

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"numbers", "acquire", "--country", "US",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if srv.quotes != 1 || srv.buys != 1 {
		t.Fatalf("quotes=%d buys=%d, want 1 and 1", srv.quotes, srv.buys)
	}
	var quote client.NumberQuoteRequest
	if err := json.Unmarshal(srv.quoteBody, &quote); err != nil {
		t.Fatalf("quote body: %v; raw=%s", err, srv.quoteBody)
	}
	if quote.CountryCode != "US" || len(quote.Capabilities) != 2 {
		t.Fatalf("quote request = %+v", quote)
	}
	if quote.NumberType == nil || *quote.NumberType != "local" {
		t.Fatalf("quote NumberType = %v", quote.NumberType)
	}
	var body client.NumberAcquireRequest
	if err := json.Unmarshal(srv.buyBody, &body); err != nil {
		t.Fatalf("buy body: %v; raw=%s", err, srv.buyBody)
	}
	if body.QuoteId.String() != quoteCheap {
		t.Fatalf("bought quote %s, want the cheapest ready offer %s", body.QuoteId, quoteCheap)
	}
	if body.CountryCode != "US" {
		t.Fatalf("CountryCode = %q", body.CountryCode)
	}
	if srv.buyKey == "" {
		t.Fatal("Idempotency-Key header missing on the purchase")
	}
	if !strings.Contains(stdout, "+14155551234") || !strings.Contains(stdout, "voice, sms") {
		t.Errorf("stdout = %q", stdout)
	}
}

func TestNumberAcquire_TypeProviderAndCapabilitiesReachTheQuote(t *testing.T) {
	bought := samplePhoneNumber("11111111-1111-1111-1111-111111111111", "+18005551234", []string{"voice"}, nil)
	srv := newAcquireServer(t, []client.CarrierOffer{sampleOffer(quoteCheap, 100, 0, "ready")}, bought)

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"numbers", "acquire", "--country", "US", "--type", "toll_free", "--provider", "telnyx", "--voice-only",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var quote client.NumberQuoteRequest
	if err := json.Unmarshal(srv.quoteBody, &quote); err != nil {
		t.Fatalf("quote body: %v", err)
	}
	if quote.NumberType == nil || *quote.NumberType != "toll_free" {
		t.Fatalf("quote NumberType = %v", quote.NumberType)
	}
	if quote.Provider == nil || *quote.Provider != "telnyx" {
		t.Fatalf("quote Provider = %v", quote.Provider)
	}
	if len(quote.Capabilities) != 1 || quote.Capabilities[0] != "voice" {
		t.Fatalf("quote Capabilities = %v", quote.Capabilities)
	}
	var body client.NumberAcquireRequest
	if err := json.Unmarshal(srv.buyBody, &body); err != nil {
		t.Fatalf("buy body: %v", err)
	}
	if body.Provider == nil || *body.Provider != "telnyx" {
		t.Fatalf("buy Provider = %v", body.Provider)
	}
}

func TestNumberAcquire_QuoteIDSkipsTheQuoteRequest(t *testing.T) {
	bought := samplePhoneNumber("11111111-1111-1111-1111-111111111111", "+14155551234", []string{"voice", "sms"}, nil)
	srv := newAcquireServer(t, nil, bought)

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"numbers", "acquire", "--country", "US", "--quote-id", quoteExpensive, "--provider", "twilio",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if srv.quotes != 0 || srv.buys != 1 {
		t.Fatalf("quotes=%d buys=%d, want 0 and 1", srv.quotes, srv.buys)
	}
	var body client.NumberAcquireRequest
	if err := json.Unmarshal(srv.buyBody, &body); err != nil {
		t.Fatalf("buy body: %v", err)
	}
	if body.QuoteId.String() != quoteExpensive {
		t.Fatalf("QuoteId = %s", body.QuoteId)
	}
	if body.Provider == nil || *body.Provider != "twilio" {
		t.Fatalf("Provider = %v", body.Provider)
	}
}

func TestNumberAcquire_NoReadyOfferDoesNotBuy(t *testing.T) {
	bought := samplePhoneNumber("11111111-1111-1111-1111-111111111111", "+14155551234", []string{"voice"}, nil)
	srv := newAcquireServer(t, []client.CarrierOffer{sampleOffer(quoteNotReady, 100, 0, "verification_required")}, bought)

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"numbers", "acquire", "--country", "US",
	)
	if err == nil || !strings.Contains(err.Error(), "no number ready to buy") {
		t.Fatalf("err = %v", err)
	}
	if srv.buys != 0 {
		t.Errorf("expected no purchase, got %d", srv.buys)
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

func TestNumberAcquire_RejectsBadFlagsBeforeNetwork(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, samplePhoneNumber("11111111-1111-1111-1111-111111111111", "+14155551234", []string{"voice"}, nil))

	for _, extra := range [][]string{
		{"--type", "satellite"},
		{"--provider", "acme"},
		{"--quote-id", "not-a-uuid"},
		{"--voice-only", "--sms-only"},
	} {
		args := append([]string{"numbers", "acquire", "--country", "US"}, extra...)
		_, _, err := runRoot(t,
			map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
			args...,
		)
		if err == nil {
			t.Errorf("expected error for %v", extra)
		}
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
	if srv.lastReq.URL.Path != "/v1/numbers" {
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
	if srv.lastReq.Method != http.MethodGet || srv.lastReq.URL.Path != "/v1/numbers/"+resp.Id.String() {
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
	if srv.lastReq.Method != http.MethodPost || srv.lastReq.URL.Path != "/v1/numbers/"+resp.Id.String()+"/enable-sms" {
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
