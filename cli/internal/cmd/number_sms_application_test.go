package cmd

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
)

const smsRepairID = "11111111-1111-1111-1111-111111111111"

func TestNumberGetSmsApplicationReadsSameAuthenticatedRow(t *testing.T) {
	calls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.Method != http.MethodGet || r.URL.Path != "/v1/verifications/"+smsRepairID+"/application" {
			t.Errorf("unexpected request %s %s", r.Method, r.URL.Path)
		}
		if r.Header.Get("Authorization") != "Bearer sk_test" {
			t.Error("missing auth")
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"schema_version":2,"subject_type":"person","fields":{"program_name":"Acme"},"address":null}`))
	}))
	t.Cleanup(srv.Close)
	stdout, _, err := runRoot(t, map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL}, "numbers", "get-sms-application", smsRepairID)
	if err != nil {
		t.Fatal(err)
	}
	if calls != 1 || !strings.Contains(stdout, "Acme") {
		t.Fatalf("calls=%d stdout=%s", calls, stdout)
	}
}

func smsAmendArgs() []string {
	return []string{"--json", "numbers", "amend-sms", smsRepairID,
		"--field", "program_name=Acme", "--field", "consent_methods=WEBSITE",
		"--address", "customer_name=Ada", "--address", "street=1 Main St",
		"--address", "city=Toronto", "--address", "region=ON",
		"--address", "postal_code=M5V 1A1", "--address", "country_code=CA"}
}

func TestNumberAmendSmsPatchesSameRowAndKeepsReadinessJSON(t *testing.T) {
	calls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.Method != http.MethodPatch || r.URL.Path != "/v1/verifications/"+smsRepairID+"/application" {
			t.Errorf("unexpected request %s %s", r.Method, r.URL.Path)
		}
		if r.Header.Get("Authorization") != "Bearer sk_test" {
			t.Error("missing auth")
		}
		var body struct {
			Fields  map[string]string `json:"fields"`
			Address map[string]string `json:"address"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatal(err)
		}
		if body.Fields["program_name"] != "Acme" || body.Address["country_code"] != "CA" {
			t.Errorf("wrong body %+v", body)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"id":"` + smsRepairID + `","state":"submitted","purpose":"sms","country_code":"US","number_type":"local","subject_type":"person","created_at":"2026-11-05T00:00:00Z","updated_at":"2026-11-05T00:00:00Z","step":"needs_information","campaign_accepted":false,"runtime_ready":false}`))
	}))
	t.Cleanup(srv.Close)
	stdout, _, err := runRoot(t, map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL}, smsAmendArgs()...)
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]any
	if err := json.Unmarshal([]byte(stdout), &got); err != nil {
		t.Fatal(err)
	}
	if calls != 1 || got["id"] != smsRepairID || got["step"] != "needs_information" || got["runtime_ready"] != false {
		t.Fatalf("calls=%d result=%s", calls, stdout)
	}
}

func TestNumberAmendSmsRefusalDoesNotCreateOrCancel(t *testing.T) {
	calls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.Method != http.MethodPatch {
			t.Errorf("unexpected method %s", r.Method)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusConflict)
		_, _ = w.Write([]byte(`{"detail":"Sender identity cannot change on this application. Contact support."}`))
	}))
	t.Cleanup(srv.Close)
	_, _, err := runRoot(t, map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL}, smsAmendArgs()...)
	if err == nil || calls != 1 {
		t.Fatalf("err=%v calls=%d", err, calls)
	}
}

func TestNumberAmendSmsValidatesEnvelopeBeforeSending(t *testing.T) {
	cases := [][]string{
		{"numbers", "amend-sms", "bad-id", "--field", "program_name=Acme"},
		{"numbers", "amend-sms", smsRepairID},
		{"numbers", "amend-sms", smsRepairID, "--field", "program_name=Acme"},
		append(smsAmendArgs(), "--field", "oversize="+strings.Repeat("x", 65537)),
	}
	for _, args := range cases {
		srv := newFakeServer(t, http.StatusInternalServerError, map[string]string{"detail": "must not call"})
		_, _, err := runRoot(t, map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL}, args...)
		if err == nil {
			t.Fatal("expected input error")
		}
		if atomic.LoadInt32(&srv.hits) != 0 {
			t.Fatal("invalid input sent a request")
		}
	}
}
