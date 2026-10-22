package cmd

import (
	"encoding/json"
	"io"
	"mime"
	"mime/multipart"
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

func samplePhoneNumber(idStr, e164 string, caps []string) client.PhoneNumberResponse {
	id := openapi_types.UUID(uuid.MustParse(idStr))
	return client.PhoneNumberResponse{
		Id:                id,
		E164:              e164,
		CountryCode:       "US",
		NumberType:        "local",
		Capabilities:      caps,
		ProvisioningState: "active",
		IsDedicated:       true,
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

func newAcquireServer(t *testing.T, offers []client.QuoteOffer, bought client.PhoneNumberResponse) *acquireServer {
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
				Offers:    offers,
				ExpiresAt: time.Now().Add(10 * time.Minute),
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

func sampleOffer(quoteID string, monthly, setup int, readiness string) client.QuoteOffer {
	id := openapi_types.UUID(uuid.MustParse(quoteID))
	return client.QuoteOffer{
		Capabilities: []string{"voice", "sms"},
		CountryCode:  "US",
		E164:         strPtr("+14155550000"),
		MonthlyCents: monthly,
		NumberType:   "local",
		QuoteId:      &id,
		Readiness:    client.QuoteOfferReadiness(readiness),
		SetupCents:   setup,
	}
}

const (
	quoteExpensive = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
	quoteCheap     = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
	quoteNotReady  = "cccccccc-cccc-cccc-cccc-cccccccccccc"
)

func TestNumberAcquire_BuysTheCheapestReadyOffer(t *testing.T) {
	bought := samplePhoneNumber("11111111-1111-1111-1111-111111111111", "+14155551234", []string{"voice", "sms"})
	srv := newAcquireServer(t, []client.QuoteOffer{
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

func TestNumberAcquire_SkipsAByRequestOffer(t *testing.T) {
	bought := samplePhoneNumber("11111111-1111-1111-1111-111111111111", "+14155551234", []string{"voice", "sms"})
	byRequest := sampleOffer(quoteNotReady, 10, 0, "ready")
	byRequest.E164 = nil
	yes := true
	byRequest.ByRequest = &yes
	srv := newAcquireServer(t, []client.QuoteOffer{byRequest, sampleOffer(quoteCheap, 100, 0, "ready")}, bought)

	if _, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"numbers", "acquire", "--country", "US",
	); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var body client.NumberAcquireRequest
	if err := json.Unmarshal(srv.buyBody, &body); err != nil {
		t.Fatalf("buy body: %v; raw=%s", err, srv.buyBody)
	}
	if body.QuoteId.String() != quoteCheap {
		t.Fatalf("bought quote %s, want %s (a by-request offer is never bought)", body.QuoteId, quoteCheap)
	}
}

func TestNumberAcquire_TypeAndCapabilitiesReachTheQuote(t *testing.T) {
	bought := samplePhoneNumber("11111111-1111-1111-1111-111111111111", "+18005551234", []string{"voice"})
	srv := newAcquireServer(t, []client.QuoteOffer{sampleOffer(quoteCheap, 100, 0, "ready")}, bought)

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"numbers", "acquire", "--country", "US", "--type", "toll_free", "--voice-only",
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
	if quote.Provider != nil {
		t.Fatalf("quote sent a carrier: %v", *quote.Provider)
	}
	if len(quote.Capabilities) != 1 || quote.Capabilities[0] != "voice" {
		t.Fatalf("quote Capabilities = %v", quote.Capabilities)
	}
	var body client.NumberAcquireRequest
	if err := json.Unmarshal(srv.buyBody, &body); err != nil {
		t.Fatalf("buy body: %v", err)
	}
	if body.Provider != nil {
		t.Fatalf("buy sent a carrier: %v", *body.Provider)
	}
}

func TestNumberAcquire_QuoteIDSkipsTheQuoteRequest(t *testing.T) {
	bought := samplePhoneNumber("11111111-1111-1111-1111-111111111111", "+14155551234", []string{"voice", "sms"})
	srv := newAcquireServer(t, nil, bought)

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"numbers", "acquire", "--country", "US", "--quote-id", quoteExpensive,
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
	if body.Provider != nil {
		t.Fatalf("buy sent a carrier: %v", *body.Provider)
	}
}

func TestNumberAcquire_ExpectedTotalCentsIsSentOnlyWhenSet(t *testing.T) {
	bought := samplePhoneNumber("11111111-1111-1111-1111-111111111111", "+14155551234", []string{"voice", "sms"})
	env := func(srv *acquireServer) map[string]string {
		return map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL}
	}

	with := newAcquireServer(t, nil, bought)
	if _, _, err := runRoot(t, env(with),
		"numbers", "acquire", "--country", "US", "--quote-id", quoteCheap, "--expected-total-cents", "150",
	); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var body client.NumberAcquireRequest
	if err := json.Unmarshal(with.buyBody, &body); err != nil {
		t.Fatalf("buy body: %v", err)
	}
	if body.ExpectedTotalCents == nil || *body.ExpectedTotalCents != 150 {
		t.Fatalf("ExpectedTotalCents = %v, want 150", body.ExpectedTotalCents)
	}

	without := newAcquireServer(t, nil, bought)
	if _, _, err := runRoot(t, env(without),
		"numbers", "acquire", "--country", "US", "--quote-id", quoteCheap,
	); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if strings.Contains(string(without.buyBody), "expected_total_cents") {
		t.Fatalf("body sent expected_total_cents without the flag: %s", without.buyBody)
	}
}

func TestNumberAcquire_NoReadyOfferDoesNotBuy(t *testing.T) {
	bought := samplePhoneNumber("11111111-1111-1111-1111-111111111111", "+14155551234", []string{"voice"})
	srv := newAcquireServer(t, []client.QuoteOffer{sampleOffer(quoteNotReady, 100, 0, "verification_required")}, bought)

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

func TestNumberAcquire_AllByRequestSaysAskSupport(t *testing.T) {
	bought := samplePhoneNumber("11111111-1111-1111-1111-111111111111", "+14155551234", []string{"voice"})
	yes := true
	ready := sampleOffer(quoteCheap, 100, 0, "ready")
	ready.E164, ready.ByRequest = nil, &yes
	blocked := sampleOffer(quoteNotReady, 100, 0, "verification_required")
	blocked.E164, blocked.ByRequest = nil, &yes
	srv := newAcquireServer(t, []client.QuoteOffer{ready, blocked}, bought)

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"numbers", "acquire", "--country", "DE",
	)
	if err == nil || !strings.Contains(err.Error(), "set up by our team") || !strings.Contains(err.Error(), "ask support") {
		t.Fatalf("err = %v", err)
	}
	if srv.buys != 0 {
		t.Errorf("expected no purchase, got %d", srv.buys)
	}
}

func TestNumberAcquire_MissingCountryFailsBeforeNetwork(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, samplePhoneNumber("11111111-1111-1111-1111-111111111111", "+14155551234", []string{"voice"}))

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
	srv := newFakeServer(t, http.StatusCreated, samplePhoneNumber("11111111-1111-1111-1111-111111111111", "+14155551234", []string{"voice"}))

	for _, extra := range [][]string{
		{"--type", "satellite"},
		{"--provider", "twilio"},
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
		samplePhoneNumber("11111111-1111-1111-1111-111111111111", "+15551110001", []string{"voice", "sms"}),
		samplePhoneNumber("22222222-2222-2222-2222-222222222221", "+15551110002", []string{"voice"}),
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
	resp := samplePhoneNumber("11111111-1111-1111-1111-111111111111", "+14155551234", []string{"voice", "sms"})
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
	if strings.Contains(stdout, "Messaging:") {
		t.Errorf("stdout shows a carrier messaging id: %q", stdout)
	}
}

func TestNumberGet_RejectsBadShape(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, samplePhoneNumber("11111111-1111-1111-1111-111111111111", "+14155551234", []string{"voice"}))

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
	resp := samplePhoneNumber("11111111-1111-1111-1111-111111111111", "+14155551234", []string{"voice", "sms"})
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
	if strings.Contains(stdout, "Messaging:") {
		t.Errorf("stdout shows a carrier messaging id: %q", stdout)
	}
}

func TestNumberGet_JSONOutput(t *testing.T) {
	resp := samplePhoneNumber("11111111-1111-1111-1111-111111111111", "+14155551234", []string{"voice", "sms"})
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

func registerSmsParts(t *testing.T, extra ...string) (map[string]string, string) {
	t.Helper()
	var gotParts map[string]string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/verifications" || r.Method != http.MethodPost {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		_, params, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
		if err != nil {
			t.Errorf("content type: %v", err)
		}
		mr := multipart.NewReader(r.Body, params["boundary"])
		gotParts = map[string]string{}
		for {
			p, err := mr.NextPart()
			if err != nil {
				break
			}
			b, _ := io.ReadAll(p)
			gotParts[p.FormName()] = string(b)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(`{"id":"11111111-1111-1111-1111-111111111111","state":"awaiting_review","purpose":"sms","country_code":"US","number_type":"local","subject_type":"business","created_at":"2026-10-13T00:00:00Z","updated_at":"2026-10-13T00:00:00Z"}`))
	}))
	t.Cleanup(srv.Close)

	args := append([]string{
		"numbers", "register-sms", "--subject", "business",
		"--field", "legal_name=Acme Inc", "--field", "ein=12-3456789",
		"--address", "city=Austin",
	}, extra...)
	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		args...,
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	return gotParts, stdout
}

func TestNumberRegisterSms_PostsPurposeSmsMultipart(t *testing.T) {
	gotParts, stdout := registerSmsParts(t)
	for _, part := range []string{"provider", "number_id"} {
		if _, ok := gotParts[part]; ok {
			t.Errorf("%s part sent unasked: %q", part, gotParts[part])
		}
	}
	tests := []struct{ part, want string }{
		{"purpose", "sms"},
		{"country_code", "US"},
		{"number_type", "local"},
		{"subject_type", "business"},
	}
	for _, tc := range tests {
		if gotParts[tc.part] != tc.want {
			t.Errorf("part %s = %q, want %q", tc.part, gotParts[tc.part], tc.want)
		}
	}
	var fields map[string]string
	if err := json.Unmarshal([]byte(gotParts["fields"]), &fields); err != nil || fields["ein"] != "12-3456789" {
		t.Errorf("fields part = %q (%v)", gotParts["fields"], err)
	}
	if !strings.Contains(gotParts["address"], `"country_code":"US"`) || !strings.Contains(gotParts["address"], `"city":"Austin"`) {
		t.Errorf("address part = %q", gotParts["address"])
	}
	if !strings.Contains(stdout, "awaiting_review") {
		t.Errorf("stdout = %q", stdout)
	}
}

func TestNumberRegisterSms_NumberIDFlagSendsPart(t *testing.T) {
	const nid = "22222222-2222-2222-2222-222222222222"
	gotParts, _ := registerSmsParts(t, "--number-id", nid)
	if gotParts["number_id"] != nid {
		t.Errorf("number_id part = %q, want %s", gotParts["number_id"], nid)
	}
}

func TestNumberConfirmSms_PostsTheCode(t *testing.T) {
	const vid = "11111111-1111-1111-1111-111111111111"
	tests := []struct {
		name     string
		code     string
		wantBody string
	}{
		{"digits", "482913", `{"code":"482913"}`},
		{"trimmed", " 4829 ", `{"code":"4829"}`},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			var gotPath, gotBody string
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				gotPath = r.Method + " " + r.URL.Path
				b, _ := io.ReadAll(r.Body)
				gotBody = strings.TrimSpace(string(b))
				w.Header().Set("Content-Type", "application/json")
				_, _ = w.Write([]byte(`{"id":"` + vid + `","state":"submitted","purpose":"sms","country_code":"US","number_type":"local","subject_type":"person","created_at":"2026-10-13T00:00:00Z","updated_at":"2026-10-13T00:00:00Z"}`))
			}))
			t.Cleanup(srv.Close)
			stdout, _, err := runRoot(t,
				map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
				"numbers", "confirm-sms", vid, "--code", tc.code,
			)
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if gotPath != "POST /v1/verifications/"+vid+"/confirm" {
				t.Errorf("route = %q", gotPath)
			}
			if gotBody != tc.wantBody {
				t.Errorf("body = %q, want %q", gotBody, tc.wantBody)
			}
			if !strings.Contains(stdout, "submitted") {
				t.Errorf("stdout = %q", stdout)
			}
		})
	}
}

func TestNumberConfirmSms_RejectsBadInput(t *testing.T) {
	tests := []struct {
		name string
		args []string
	}{
		{"no code", []string{"numbers", "confirm-sms", "11111111-1111-1111-1111-111111111111"}},
		{"bad id", []string{"numbers", "confirm-sms", "xyz", "--code", "123456"}},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			srv := newFakeServer(t, http.StatusOK, nil)
			_, _, err := runRoot(t,
				map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
				tc.args...,
			)
			if err == nil {
				t.Fatal("expected error")
			}
			if hits := atomic.LoadInt32(&srv.hits); hits != 0 {
				t.Errorf("expected 0 HTTP calls, got %d", hits)
			}
		})
	}
}

func TestNumberCancelSms_DeletesTheVerification(t *testing.T) {
	const vid = "11111111-1111-1111-1111-111111111111"
	var gotRoute string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotRoute = r.Method + " " + r.URL.Path
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"id":"` + vid + `","state":"cancelled","purpose":"sms","country_code":"US","number_type":"local","subject_type":"business","created_at":"2026-10-13T00:00:00Z","updated_at":"2026-10-13T00:00:00Z"}`))
	}))
	t.Cleanup(srv.Close)
	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"numbers", "cancel-sms", vid,
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if gotRoute != "DELETE /v1/verifications/"+vid {
		t.Errorf("route = %q", gotRoute)
	}
	if !strings.Contains(stdout, "cancelled") {
		t.Errorf("stdout = %q", stdout)
	}
}

func TestNumberCancelSms_RejectsBadID(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, nil)
	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"numbers", "cancel-sms", "xyz",
	)
	if err == nil {
		t.Fatal("expected error")
	}
	if hits := atomic.LoadInt32(&srv.hits); hits != 0 {
		t.Errorf("expected 0 HTTP calls, got %d", hits)
	}
}
