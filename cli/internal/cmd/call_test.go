package cmd

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	openapi_types "github.com/oapi-codegen/runtime/types"

	"github.com/google/uuid"

	"github.com/hail-hq/hail/cli/internal/client"
)

// fakeServer wraps httptest with a request counter so "no HTTP call made" can
// be asserted in validation-error tests.
type fakeServer struct {
	*httptest.Server
	hits     int32
	lastReq  *http.Request
	lastBody []byte
}

func newFakeServer(t *testing.T, status int, response any) *fakeServer {
	t.Helper()
	fs := &fakeServer{}
	fs.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&fs.hits, 1)
		body, _ := io.ReadAll(r.Body)
		fs.lastReq = r.Clone(r.Context())
		fs.lastBody = body
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_ = json.NewEncoder(w).Encode(response)
	}))
	t.Cleanup(fs.Close)
	return fs
}

// sampleResponse returns a CallResponse populated with realistic data.
func sampleResponse() client.CallResponse {
	id := openapi_types.UUID(uuid.MustParse("11111111-1111-1111-1111-111111111111"))
	orgID := openapi_types.UUID(uuid.MustParse("22222222-2222-2222-2222-222222222222"))
	now := time.Date(2026, 4, 22, 12, 0, 0, 0, time.UTC)
	return client.CallResponse{
		Id:              id,
		OrganizationId:  orgID,
		ConversationId:  nil,
		FromE164:        "+14155551234",
		ToE164:          "+15551234567",
		Direction:       client.CallResponseDirectionOutbound,
		Status:          client.CallResponseStatusDialing,
		EndReason:       nil,
		ProviderCallSid: nil,
		LivekitRoom:     nil,
		InitialPrompt:   nil,
		RecordingS3Key:  nil,
		RequestedAt:     now,
		StartedAt:       nil,
		AnsweredAt:      nil,
		EndedAt:         nil,
	}
}

// runRoot drives a synthetic invocation of the root command with controlled
// stdout/stderr/env.
func runRoot(t *testing.T, env map[string]string, args ...string) (stdoutStr, stderrStr string, err error) {
	t.Helper()
	stdout := &bytes.Buffer{}
	stderr := &bytes.Buffer{}
	getenv := func(k string) string { return env[k] }
	root := NewRootCmd(stdout, stderr, getenv)
	root.SetArgs(args)
	err = root.Execute()
	return stdout.String(), stderr.String(), err
}

func TestCallSubcommand_ModeA_HappyPath(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleResponse())

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"call", "+15551234567", "--prompt", "you are a polite agent", "--recipient-consent",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if got := atomic.LoadInt32(&srv.hits); got != 1 {
		t.Fatalf("expected 1 request, got %d", got)
	}
	if srv.lastReq.Method != http.MethodPost || srv.lastReq.URL.Path != "/calls" {
		t.Fatalf("unexpected route: %s %s", srv.lastReq.Method, srv.lastReq.URL.Path)
	}
	if h := srv.lastReq.Header.Get("Authorization"); h != "Bearer sk_test" {
		t.Fatalf("Authorization header = %q", h)
	}
	if h := srv.lastReq.Header.Get("Idempotency-Key"); h == "" {
		t.Fatal("Idempotency-Key header missing")
	}

	var body client.CallCreate
	if err := json.Unmarshal(srv.lastBody, &body); err != nil {
		t.Fatalf("body parse: %v; raw=%s", err, srv.lastBody)
	}
	if body.To != "+15551234567" {
		t.Fatalf("To = %q", body.To)
	}
	if body.SystemPrompt == nil || *body.SystemPrompt != "you are a polite agent" {
		t.Fatalf("SystemPrompt = %v", body.SystemPrompt)
	}
	if body.Llm != nil {
		t.Fatalf("Llm should be nil, got %+v", body.Llm)
	}

	if !strings.Contains(stdout, "11111111-1111-1111-1111-111111111111") {
		t.Errorf("stdout missing call id: %q", stdout)
	}
	if !strings.Contains(stdout, "Status:") || !strings.Contains(stdout, "dialing") {
		t.Errorf("stdout missing status: %q", stdout)
	}
}

func TestCallSubcommand_ModeB_BringYourOwnLLM(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleResponse())

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"call", "+15551234567",
		"--llm-url", "https://api.openai.com/v1",
		"--llm-key", "sk-openai",
		"--llm-model", "gpt-4o-mini",
		"--recipient-consent",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	var body client.CallCreate
	if err := json.Unmarshal(srv.lastBody, &body); err != nil {
		t.Fatalf("body parse: %v", err)
	}
	if body.SystemPrompt != nil {
		t.Errorf("SystemPrompt should be nil, got %v", *body.SystemPrompt)
	}
	if body.Llm == nil {
		t.Fatal("Llm should be set")
	}
	if body.Llm.BaseUrl != "https://api.openai.com/v1" || body.Llm.ApiKey != "sk-openai" || body.Llm.Model != "gpt-4o-mini" {
		t.Errorf("Llm = %+v", body.Llm)
	}
}

func TestCallSubcommand_RejectsBothModes(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleResponse())

	_, stderr, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"call", "+15551234567",
		"--prompt", "hi",
		"--llm-url", "https://api.openai.com/v1",
		"--llm-key", "k",
		"--llm-model", "m",
		"--recipient-consent",
	)
	if !errors.Is(err, errInvalidInputs) {
		t.Fatalf("want errInvalidInputs, got %v", err)
	}
	if !strings.Contains(stderr, "--prompt and --llm-* are mutually exclusive") {
		t.Errorf("stderr = %q", stderr)
	}
	if hits := atomic.LoadInt32(&srv.hits); hits != 0 {
		t.Errorf("expected 0 HTTP calls, got %d", hits)
	}
}

func TestCallSubcommand_RejectsNeitherMode(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleResponse())

	_, stderr, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"call", "+15551234567", "--recipient-consent",
	)
	if !errors.Is(err, errInvalidInputs) {
		t.Fatalf("want errInvalidInputs, got %v", err)
	}
	if !strings.Contains(stderr, "either --prompt or") {
		t.Errorf("stderr = %q", stderr)
	}
	if hits := atomic.LoadInt32(&srv.hits); hits != 0 {
		t.Errorf("expected 0 HTTP calls, got %d", hits)
	}
}

func TestCallSubcommand_FromAndFirstMessageFlow(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleResponse())

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"call", "+15551234567",
		"--prompt", "hi",
		"--from", "+14155550000",
		"--first-message", "Hello, this is Hail.",
		"--recipient-consent",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	var body client.CallCreate
	if err := json.Unmarshal(srv.lastBody, &body); err != nil {
		t.Fatalf("body parse: %v", err)
	}
	if body.From == nil || *body.From != "+14155550000" {
		t.Errorf("From = %v", body.From)
	}
	if body.FirstMessage == nil || *body.FirstMessage != "Hello, this is Hail." {
		t.Errorf("FirstMessage = %v", body.FirstMessage)
	}
}

func TestCallSubcommand_LanguageFlag(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleResponse())

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"call", "+15551234567",
		"--prompt", "hi",
		"--language", "fr",
		"--recipient-consent",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	var body client.CallCreate
	if err := json.Unmarshal(srv.lastBody, &body); err != nil {
		t.Fatalf("body parse: %v", err)
	}
	if body.VoiceConfig == nil || body.VoiceConfig.Language == nil || *body.VoiceConfig.Language != "fr" {
		t.Errorf("VoiceConfig.Language = %v, want fr", body.VoiceConfig)
	}
}

func TestCallSubcommand_AiDisclosureFlag(t *testing.T) {
	t.Run("explicit false sent", func(t *testing.T) {
		srv := newFakeServer(t, http.StatusCreated, sampleResponse())
		_, _, err := runRoot(t,
			map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
			"call", "+15551234567", "--prompt", "hi", "--recipient-consent",
			"--ai-disclosure=false",
		)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		var body client.CallCreate
		if err := json.Unmarshal(srv.lastBody, &body); err != nil {
			t.Fatalf("body parse: %v", err)
		}
		if body.AiDisclosure == nil || *body.AiDisclosure != false {
			t.Errorf("AiDisclosure = %v, want false", body.AiDisclosure)
		}
	})
	t.Run("omitted stays server-default", func(t *testing.T) {
		srv := newFakeServer(t, http.StatusCreated, sampleResponse())
		_, _, err := runRoot(t,
			map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
			"call", "+15551234567", "--prompt", "hi", "--recipient-consent",
		)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		var body client.CallCreate
		if err := json.Unmarshal(srv.lastBody, &body); err != nil {
			t.Fatalf("body parse: %v", err)
		}
		if body.AiDisclosure != nil {
			t.Errorf("AiDisclosure = %v, want omitted", *body.AiDisclosure)
		}
	})
}

func TestCallSubcommand_ToolsFlag(t *testing.T) {
	t.Run("explicit list", func(t *testing.T) {
		srv := newFakeServer(t, http.StatusCreated, sampleResponse())
		_, _, err := runRoot(t,
			map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
			"call", "+15551234567", "--prompt", "hi", "--recipient-consent",
			"--tools", "end_call,send_sms",
		)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		var body client.CallCreate
		if err := json.Unmarshal(srv.lastBody, &body); err != nil {
			t.Fatalf("body parse: %v", err)
		}
		if body.Tools == nil || len(*body.Tools) != 2 || (*body.Tools)[0] != "end_call" || (*body.Tools)[1] != "send_sms" {
			t.Errorf("Tools = %v, want [end_call send_sms]", body.Tools)
		}
	})
	t.Run("none disables all", func(t *testing.T) {
		srv := newFakeServer(t, http.StatusCreated, sampleResponse())
		_, _, err := runRoot(t,
			map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
			"call", "+15551234567", "--prompt", "hi", "--recipient-consent",
			"--tools", "none",
		)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		var raw map[string]any
		if err := json.Unmarshal(srv.lastBody, &raw); err != nil {
			t.Fatalf("body parse: %v", err)
		}
		tools, present := raw["tools"]
		if !present {
			t.Fatalf("tools missing from body, want []")
		}
		if list, ok := tools.([]any); !ok || len(list) != 0 {
			t.Errorf("tools = %v, want []", tools)
		}
	})
	t.Run("omitted stays absent", func(t *testing.T) {
		srv := newFakeServer(t, http.StatusCreated, sampleResponse())
		_, _, err := runRoot(t,
			map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
			"call", "+15551234567", "--prompt", "hi", "--recipient-consent",
		)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		var raw map[string]any
		if err := json.Unmarshal(srv.lastBody, &raw); err != nil {
			t.Fatalf("body parse: %v", err)
		}
		if _, present := raw["tools"]; present {
			t.Errorf("tools should be omitted when flag not passed")
		}
	})
}

func TestCallSubcommand_PropagatesIdempotencyKey(t *testing.T) {
	t.Run("explicit", func(t *testing.T) {
		srv := newFakeServer(t, http.StatusCreated, sampleResponse())
		_, _, err := runRoot(t,
			map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
			"call", "+15551234567", "--prompt", "hi", "--idempotency-key", "deadbeef-1234", "--recipient-consent",
		)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if h := srv.lastReq.Header.Get("Idempotency-Key"); h != "deadbeef-1234" {
			t.Errorf("Idempotency-Key = %q, want deadbeef-1234", h)
		}
	})
	t.Run("auto-uuid", func(t *testing.T) {
		srv := newFakeServer(t, http.StatusCreated, sampleResponse())
		_, _, err := runRoot(t,
			map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
			"call", "+15551234567", "--prompt", "hi", "--recipient-consent",
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

func TestCallSubcommand_HandlesAPIError(t *testing.T) {
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
		"call", "+15551234567", "--prompt", "hi", "--recipient-consent",
	)
	if err == nil {
		t.Fatal("expected error from server, got nil")
	}
	if !strings.Contains(err.Error(), "must be E.164") {
		t.Errorf("error = %v; expected detail message", err)
	}
}

func TestCallSubcommand_JSONOutput(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleResponse())

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"--json", "call", "+15551234567", "--prompt", "hi", "--recipient-consent",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var got client.CallResponse
	if err := json.Unmarshal([]byte(stdout), &got); err != nil {
		t.Fatalf("stdout is not valid JSON: %v\n%s", err, stdout)
	}
	if got.Id != openapi_types.UUID(uuid.MustParse("11111111-1111-1111-1111-111111111111")) {
		t.Errorf("Id = %v", got.Id)
	}
}

// TestSynonyms_Topology pins the synonym wiring so future refactors can't
// silently drop a documented alias.
func TestSynonyms_Topology(t *testing.T) {
	root := NewRootCmd(&bytes.Buffer{}, &bytes.Buffer{}, func(string) string { return "" })

	if c, _, err := root.Find([]string{"login"}); err != nil || c.Name() != "login" {
		t.Errorf("hail login: cmd=%v err=%v", c, err)
	}
	if c, _, err := root.Find([]string{"auth", "login"}); err != nil || c.Name() != "login" {
		t.Errorf("hail auth login: cmd=%v err=%v", c, err)
	}

	call, _, err := root.Find([]string{"call"})
	if err != nil {
		t.Fatalf("hail call: %v", err)
	}
	wantAliases := map[string]string{"list": "ls", "status": "get"}
	for sub, alias := range wantAliases {
		found := false
		for _, c := range call.Commands() {
			if c.Name() == sub {
				for _, a := range c.Aliases {
					if a == alias {
						found = true
					}
				}
			}
		}
		if !found {
			t.Errorf("hail call %s: alias %q not registered", sub, alias)
		}
	}

	loginCmd, _, err := root.Find([]string{"login"})
	if err != nil {
		t.Fatalf("login Find: %v", err)
	}
	got := string(loginCmd.Flags().GetNormalizeFunc()(loginCmd.Flags(), "login-url"))
	if got != "auth-url" {
		t.Errorf("--login-url normalized to %q, want auth-url", got)
	}
	if got := string(loginCmd.Flags().GetNormalizeFunc()(loginCmd.Flags(), "auth-url")); got != "auth-url" {
		t.Errorf("--auth-url normalized to %q, want auth-url", got)
	}
}

// TestPersistentFlags_PositionFlexibility pins cobra's behavior: --api-url and
// --api-key are persistent root flags, so they must be accepted in any order —
// before the subcommand, after the subcommand, or after the positional. Guards
// against accidental regressions (e.g. someone moving the flag definition off
// root.PersistentFlags).
func TestPersistentFlags_PositionFlexibility(t *testing.T) {
	tests := []struct {
		name    string
		argsFor func(url string) []string
	}{
		{"before subcommand", func(u string) []string {
			return []string{"--api-url", u, "--api-key", "sk_test", "call", "+15551234567", "--prompt", "hi", "--recipient-consent"}
		}},
		{"after subcommand", func(u string) []string {
			return []string{"call", "--api-url", u, "--api-key", "sk_test", "+15551234567", "--prompt", "hi", "--recipient-consent"}
		}},
		{"after positional", func(u string) []string {
			return []string{"call", "+15551234567", "--prompt", "hi", "--recipient-consent", "--api-url", u, "--api-key", "sk_test"}
		}},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			srv := newFakeServer(t, http.StatusCreated, sampleResponse())

			_, _, err := runRoot(t, map[string]string{}, tc.argsFor(srv.URL)...)
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if got := atomic.LoadInt32(&srv.hits); got != 1 {
				t.Fatalf("expected 1 request to %s, got %d (flag did not bind)", srv.URL, got)
			}
			if h := srv.lastReq.Header.Get("Authorization"); h != "Bearer sk_test" {
				t.Fatalf("Authorization header = %q; --api-key did not propagate", h)
			}
		})
	}
}

func TestCallSubcommand_MissingPositional_PrintsHelp(t *testing.T) {
	_, stderr, err := runRoot(t, map[string]string{"HAIL_API_KEY": "sk_test"}, "call")
	if !errors.Is(err, errInvalidInputs) {
		t.Fatalf("want errInvalidInputs, got %v", err)
	}
	if !strings.Contains(stderr, "missing required: <to-number>") {
		t.Fatalf("missing arg name: %q", stderr)
	}
}

func TestCallSubcommand_MissingAPIKey(t *testing.T) {
	// loadCredentials falls back to ~/.hail/credentials.json via
	// os.UserHomeDir(), which reads $HOME on unix. Point HOME at an empty
	// temp dir so a developer's real credentials file can't satisfy the
	// API-key lookup and mask the missing-key error.
	t.Setenv("HOME", t.TempDir())

	srv := newFakeServer(t, http.StatusCreated, sampleResponse())

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_URL": srv.URL}, // no HAIL_API_KEY
		"call", "+15551234567", "--prompt", "hi", "--recipient-consent",
	)
	if err == nil {
		t.Fatal("expected error, got nil")
	}
	if !errors.Is(err, errNotAuthenticated) {
		t.Errorf("error = %v", err)
	}
	if hits := atomic.LoadInt32(&srv.hits); hits != 0 {
		t.Errorf("expected 0 HTTP calls, got %d", hits)
	}
}

func TestCallSubcommand_SendsConsentFlags(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleResponse())

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"call", "+15551234567",
		"--prompt", "you are a polite agent",
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
	if body["message_type"] != "marketing" {
		t.Errorf("message_type = %v, want marketing", body["message_type"])
	}
}

// TestCallSubcommand_RecipientConsent_OmittedFailsBeforeNetwork pins the
// MarkFlagRequired("recipient-consent") gap: omitting the flag entirely
// fails in PreRunE (requireMarkedFlags), before any HTTP request is made.
func TestCallSubcommand_RecipientConsent_OmittedFailsBeforeNetwork(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleResponse())

	_, stderr, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"call", "+15551234567", "--prompt", "you are a polite agent",
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

// TestCallSubcommand_RecipientConsent_FalseFailsBeforeNetwork pins the gap
// MarkFlagRequired alone cannot close: `--recipient-consent=false` sets
// pflag.Changed, satisfying ValidateRequiredFlags, but the API requires the
// value to actually be true. requireTrueFlag (called first in runCall)
// catches this and fails before any HTTP request is made.
func TestCallSubcommand_RecipientConsent_FalseFailsBeforeNetwork(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleResponse())

	_, stderr, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"call", "+15551234567", "--prompt", "you are a polite agent", "--recipient-consent=false",
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
