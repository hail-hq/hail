package cmd

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

const (
	testConnID   = "33333333-3333-3333-3333-333333333333"
	testNumberID = "44444444-4444-4444-4444-444444444444"
)

func sampleSipConnection(withPassword bool) map[string]any {
	c := map[string]any{
		"id":               testConnID,
		"label":            "Main office",
		"default_region":   "US",
		"sip_host":         "example.sip.livekit.cloud",
		"username":         "hail-user-1",
		"password_last4":   "wxyz",
		"pbx_host":         "pbx.example.com",
		"pbx_port":         5060,
		"pbx_transport":    "auto",
		"pbx_username":     nil,
		"pbx_password_set": false,
		"created_at":       "2026-11-25T12:00:00Z",
	}
	if withPassword {
		c["password"] = "abcdefghijklmnopqrstuvwxyz"
	}
	return c
}

func TestSipConnectionsCreatePrintsPasswordOnce(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleSipConnection(true))
	stdout, stderr, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"sip-connections", "create", "--label", "Main office", "--region", "US",
		"--pbx-host", "pbx.example.com",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if srv.lastReq.Method != http.MethodPost || srv.lastReq.URL.Path != "/v1/sip-connections" {
		t.Fatalf("route: %s %s", srv.lastReq.Method, srv.lastReq.URL.Path)
	}
	var body map[string]any
	if err := json.Unmarshal(srv.lastBody, &body); err != nil {
		t.Fatalf("body: %v", err)
	}
	if body["label"] != "Main office" || body["default_region"] != "US" || body["pbx_host"] != "pbx.example.com" {
		t.Errorf("body = %s", srv.lastBody)
	}
	if strings.Count(stdout, "abcdefghijklmnopqrstuvwxyz") != 1 {
		t.Errorf("password not printed exactly once: %q", stdout)
	}
	if !strings.Contains(stdout, "example.sip.livekit.cloud") || !strings.Contains(stdout, "hail-user-1") {
		t.Errorf("stdout missing trunk fields: %q", stdout)
	}
	if !strings.Contains(stderr, "phone system's trunk") {
		t.Errorf("stderr missing shown-once note: %q", stderr)
	}
}

func TestSipConnectionsVerifyConnected(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, map[string]any{
		"state": "active", "reason": nil, "call_id": nil,
	})
	stdout, stderr, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"sip-connections", "verify", testConnID, testNumberID,
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	want := "/v1/sip-connections/" + testConnID + "/numbers/" + testNumberID + "/verify"
	if srv.lastReq.URL.Path != want {
		t.Fatalf("path = %s", srv.lastReq.URL.Path)
	}
	if !strings.Contains(stdout, "Connected") {
		t.Errorf("stdout = %q", stdout)
	}
	if !strings.Contains(stderr, "up to 2 minutes") {
		t.Errorf("stderr = %q", stderr)
	}
}

func TestSipConnectionsVerifyNoCallArrived(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, map[string]any{
		"state": "unverified", "reason": "no_call_arrived", "call_id": nil,
	})
	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"sip-connections", "verify", testConnID, testNumberID,
	)
	if err == nil {
		t.Fatal("want an error (exit 1)")
	}
	if !strings.Contains(err.Error(), "No call arrived. Check the SIP trunk and its username and password, and that a routing rule sends this number to Hail with + and the country code.") {
		t.Errorf("err = %v", err)
	}
}

// TestSipConnectionsVerifyNoShortTimeout guards against a client timeout
// shorter than the check: the server answers after 3 s.
func TestSipConnectionsVerifyNoShortTimeout(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(3 * time.Second)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"state":"active","reason":null,"call_id":null}`))
	}))
	t.Cleanup(srv.Close)
	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"sip-connections", "verify", testConnID, testNumberID,
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestSipConnectionsAddNumber(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, map[string]any{
		"id": testNumberID, "e164": "+14155550100", "country_code": "US",
		"number_type": "local", "capabilities": []string{"voice"},
		"provisioning_state": "unverified", "is_dedicated": true,
		"source": "connected", "sip_connection_id": testConnID,
	})
	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"sip-connections", "add-number", testConnID, "+14155550100",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var body map[string]any
	_ = json.Unmarshal(srv.lastBody, &body)
	if body["e164"] != "+14155550100" {
		t.Errorf("body = %s", srv.lastBody)
	}
	if !strings.Contains(stdout, "unverified") || !strings.Contains(stdout, "connected") {
		t.Errorf("stdout = %q", stdout)
	}
}

func TestSipConnectionsUpdateClearHost(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, sampleSipConnection(false))
	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"sip-connections", "update", testConnID, "--clear-pbx-host",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if string(srv.lastBody) != `{"pbx_host":null}` {
		t.Errorf("body = %s", srv.lastBody)
	}
}

func TestSipConnectionsListPrintsNoPassword(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, map[string]any{
		"items": []any{sampleSipConnection(false)}, "next_cursor": nil,
	})
	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"sip-connections", "list",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(stdout, "Main office") || !strings.Contains(stdout, testConnID) {
		t.Errorf("stdout = %q", stdout)
	}
}

func TestSipConnectionsCreatePasswordFromStdin(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleSipConnection(true))
	_, _, err := runRootStdin(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"s3cret-from-stdin\n",
		"sip-connections", "create", "--label", "Main office", "--region", "US",
		"--pbx-host", "pbx.example.com", "--pbx-username", "hail", "--pbx-password", "-",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var body map[string]any
	_ = json.Unmarshal(srv.lastBody, &body)
	if body["pbx_password"] != "s3cret-from-stdin" {
		t.Errorf("pbx_password = %v", body["pbx_password"])
	}
}

func TestSipConnectionsUpdateRefusesSetAndClear(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, sampleSipConnection(false))
	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"sip-connections", "update", testConnID, "--pbx-host", "pbx.example.com", "--clear-pbx-host",
	)
	if err == nil {
		t.Fatal("want an error for --pbx-host with --clear-pbx-host")
	}
	if srv.hits != 0 {
		t.Fatalf("server called %d times", srv.hits)
	}
}
