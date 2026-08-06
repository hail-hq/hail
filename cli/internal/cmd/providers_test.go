package cmd

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/hail-hq/hail/cli/internal/client"
)

// runRootStdin is runRoot with an injected stdin, for the `--key -` path.
// Cobra threads the root command's input down to subcommands via
// cmd.InOrStdin(), which is what providers.go reads — so the real os.Stdin
// is never touched in tests.
func runRootStdin(t *testing.T, env map[string]string, stdin string, args ...string) (stdoutStr, stderrStr string, err error) {
	t.Helper()
	stdout := &bytes.Buffer{}
	stderr := &bytes.Buffer{}
	getenv := func(k string) string { return env[k] }
	root := NewRootCmd(stdout, stderr, getenv)
	root.SetIn(strings.NewReader(stdin))
	root.SetArgs(args)
	err = root.Execute()
	return stdout.String(), stderr.String(), err
}

func sampleProviderEntry() client.ProviderConfigEntry {
	last4 := "ABCD"
	setAt := "2026-08-06T12:00:00+00:00"
	return client.ProviderConfigEntry{
		Layer:           client.Llm,
		Provider:        "openai-compatible",
		KeyLast4:        &last4,
		KeySetAt:        &setAt,
		Params:          map[string]interface{}{"model": "my-model", "base_url": "https://llm.example.com/v1"},
		FallbackEnabled: false,
		IsActive:        true,
	}
}

func decodeBody(t *testing.T, raw []byte) map[string]interface{} {
	t.Helper()
	var got map[string]interface{}
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatalf("request body is not JSON: %v (%q)", err, string(raw))
	}
	return got
}

// --------------------------------------------------------------------------- //
// list
// --------------------------------------------------------------------------- //

func TestProvidersList_HappyPath(t *testing.T) {
	listResp := client.ProviderConfigListResponse{
		Providers: []client.ProviderConfigEntry{sampleProviderEntry()},
	}
	srv := newFakeServer(t, http.StatusOK, listResp)

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"providers", "list",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if srv.lastReq.Method != http.MethodGet || srv.lastReq.URL.Path != "/providers" {
		t.Fatalf("unexpected route: %s %s", srv.lastReq.Method, srv.lastReq.URL.Path)
	}
	for _, want := range []string{"LAYER", "PROVIDER", "openai-compatible", "…ABCD", "my-model"} {
		if !strings.Contains(stdout, want) {
			t.Errorf("stdout missing %q: %q", want, stdout)
		}
	}
}

func TestProvidersList_Empty(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, client.ProviderConfigListResponse{})

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"providers", "list",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(stdout, "no saved providers") {
		t.Errorf("stdout = %q", stdout)
	}
}

// --------------------------------------------------------------------------- //
// set
// --------------------------------------------------------------------------- //

func TestProvidersSet_RequestBody(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, sampleProviderEntry())

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"providers", "set", "llm",
		"--provider", "openai-compatible",
		"--model", "my-model",
		"--base-url", "https://llm.example.com/v1",
		"--key", "sk-provider-ABCD",
		"--fallback",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if srv.lastReq.Method != http.MethodPut || srv.lastReq.URL.Path != "/providers/llm" {
		t.Fatalf("unexpected route: %s %s", srv.lastReq.Method, srv.lastReq.URL.Path)
	}

	body := decodeBody(t, srv.lastBody)
	if body["provider"] != "openai-compatible" {
		t.Errorf("provider = %v", body["provider"])
	}
	if body["api_key"] != "sk-provider-ABCD" {
		t.Errorf("api_key = %v", body["api_key"])
	}
	if body["fallback_enabled"] != true {
		t.Errorf("fallback_enabled = %v", body["fallback_enabled"])
	}
	params, ok := body["params"].(map[string]interface{})
	if !ok {
		t.Fatalf("params missing/not an object: %v", body["params"])
	}
	if params["model"] != "my-model" || params["base_url"] != "https://llm.example.com/v1" {
		t.Errorf("params = %v", params)
	}
	if !strings.Contains(stdout, "Provider saved") {
		t.Errorf("stdout = %q", stdout)
	}
}

func TestProvidersSet_OmitsKeyAndBaseURLWhenUnset(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, sampleProviderEntry())

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"providers", "set", "tts", "--provider", "cartesia", "--model", "sonic-2",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	body := decodeBody(t, srv.lastBody)
	// No api_key at all means "keep the stored key" — sending null would be
	// a different request.
	if _, present := body["api_key"]; present {
		t.Errorf("api_key must be omitted when --key is unset: %v", body)
	}
	if body["fallback_enabled"] != false {
		t.Errorf("fallback_enabled = %v", body["fallback_enabled"])
	}
	params := body["params"].(map[string]interface{})
	if _, present := params["base_url"]; present {
		t.Errorf("base_url must be omitted when unset: %v", params)
	}
	if params["model"] != "sonic-2" {
		t.Errorf("params = %v", params)
	}
}

func TestProvidersSet_KeyFromStdin(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, sampleProviderEntry())

	_, _, err := runRootStdin(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"sk-from-stdin-WXYZ\n",
		"providers", "set", "llm", "--provider", "anthropic", "--model", "m", "--key", "-",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	body := decodeBody(t, srv.lastBody)
	if body["api_key"] != "sk-from-stdin-WXYZ" {
		t.Errorf("api_key = %v, want the trimmed stdin value", body["api_key"])
	}
}

func TestProvidersSet_KeyFromEmptyStdinFails(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, sampleProviderEntry())

	_, _, err := runRootStdin(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"",
		"providers", "set", "llm", "--provider", "anthropic", "--model", "m", "--key", "-",
	)
	if err == nil {
		t.Fatal("expected an error for an empty stdin key")
	}
	if !strings.Contains(err.Error(), "stdin was empty") {
		t.Errorf("err = %v", err)
	}
	if got := atomic.LoadInt32(&srv.hits); got != 0 {
		t.Errorf("no request should have been sent, got %d", got)
	}
}

func TestProvidersSet_RequiresProviderAndModel(t *testing.T) {
	for _, args := range [][]string{
		{"providers", "set", "llm", "--model", "m"},
		{"providers", "set", "llm", "--provider", "anthropic"},
		{"providers", "set", "--provider", "anthropic", "--model", "m"},
	} {
		_, _, err := runRoot(t,
			map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": "http://127.0.0.1:1"},
			args...,
		)
		if err == nil {
			t.Errorf("%v: expected an error", args)
		}
	}
}

func TestProvidersSet_APIErrorSurfaces(t *testing.T) {
	srv := newFakeServer(t, http.StatusNotFound, map[string]string{"detail": "unknown layer 'vision'"})

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"providers", "set", "vision", "--provider", "cartesia", "--model", "m",
	)
	if err == nil {
		t.Fatal("expected an error")
	}
	if !strings.Contains(err.Error(), "unknown layer") {
		t.Errorf("err = %v", err)
	}
}

// --------------------------------------------------------------------------- //
// delete
// --------------------------------------------------------------------------- //

func TestProvidersDelete_HappyPath(t *testing.T) {
	srv := newFakeServer(t, http.StatusNoContent, nil)

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"providers", "delete", "tts", "cartesia",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if srv.lastReq.Method != http.MethodDelete || srv.lastReq.URL.Path != "/providers/tts/cartesia" {
		t.Fatalf("unexpected route: %s %s", srv.lastReq.Method, srv.lastReq.URL.Path)
	}
	if !strings.Contains(stdout, "deleted") {
		t.Errorf("stdout = %q", stdout)
	}
}

func TestProvidersDelete_NeedsBothArgs(t *testing.T) {
	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": "http://127.0.0.1:1"},
		"providers", "delete", "tts",
	)
	if err == nil {
		t.Fatal("expected an error for a missing <provider> argument")
	}
}

// --------------------------------------------------------------------------- //
// activate
// --------------------------------------------------------------------------- //

func TestProvidersActivate_RequestBody(t *testing.T) {
	entry := sampleProviderEntry()
	entry.Layer = client.Tts
	entry.Provider = "cartesia"
	srv := newFakeServer(t, http.StatusOK, entry)

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"providers", "activate", "tts", "--provider", "cartesia",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if srv.lastReq.Method != http.MethodPost || srv.lastReq.URL.Path != "/providers/tts/activate" {
		t.Fatalf("unexpected route: %s %s", srv.lastReq.Method, srv.lastReq.URL.Path)
	}
	if body := decodeBody(t, srv.lastBody); body["provider"] != "cartesia" {
		t.Errorf("body = %v", body)
	}
	if !strings.Contains(stdout, "Provider activated") {
		t.Errorf("stdout = %q", stdout)
	}
}

func TestProvidersActivate_RequiresProvider(t *testing.T) {
	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": "http://127.0.0.1:1"},
		"providers", "activate", "tts",
	)
	if err == nil {
		t.Fatal("expected an error for a missing --provider")
	}
}

// --------------------------------------------------------------------------- //
// test
// --------------------------------------------------------------------------- //

func TestProvidersTest_EmptyBodyByDefault(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, client.ProviderValidateResult{Status: "valid"})

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"providers", "test", "llm",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if srv.lastReq.Method != http.MethodPost || srv.lastReq.URL.Path != "/providers/llm/validate" {
		t.Fatalf("unexpected route: %s %s", srv.lastReq.Method, srv.lastReq.URL.Path)
	}
	if body := decodeBody(t, srv.lastBody); len(body) != 0 {
		t.Errorf("body = %v, want {}", body)
	}
	if !strings.Contains(stdout, "valid") {
		t.Errorf("stdout = %q", stdout)
	}
}

func TestProvidersTest_ProviderFlagInBody(t *testing.T) {
	msg := "401 from provider"
	srv := newFakeServer(t, http.StatusOK, client.ProviderValidateResult{Status: "invalid", Message: &msg})

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"providers", "test", "stt", "--provider", "deepgram",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if body := decodeBody(t, srv.lastBody); body["provider"] != "deepgram" {
		t.Errorf("body = %v", body)
	}
	if !strings.Contains(stdout, "invalid") || !strings.Contains(stdout, msg) {
		t.Errorf("stdout = %q", stdout)
	}
}

// --------------------------------------------------------------------------- //
// key handling unit
// --------------------------------------------------------------------------- //

func TestResolveProviderKey(t *testing.T) {
	cases := []struct {
		name  string
		flag  string
		stdin string
		want  string
		fail  bool
	}{
		{name: "literal", flag: "sk-literal", want: "sk-literal"},
		{name: "unset", flag: "", want: ""},
		{name: "stdin trailing newline", flag: "-", stdin: "sk-stdin\n", want: "sk-stdin"},
		{name: "stdin crlf", flag: "-", stdin: "sk-stdin\r\n", want: "sk-stdin"},
		{name: "stdin no newline", flag: "-", stdin: "sk-stdin", want: "sk-stdin"},
		{name: "stdin empty", flag: "-", stdin: "", fail: true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var r io.Reader = strings.NewReader(tc.stdin)
			got, err := resolveProviderKey(tc.flag, r)
			if tc.fail {
				if err == nil {
					t.Fatal("expected an error")
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if got != tc.want {
				t.Errorf("got %q, want %q", got, tc.want)
			}
		})
	}
}
