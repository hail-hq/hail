package cmd

import (
	"bytes"
	"encoding/json"
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
		Direction:       client.Outbound,
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
		"call", "+15551234567", "--prompt", "you are a polite agent",
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

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"call", "+15551234567",
		"--prompt", "hi",
		"--llm-url", "https://api.openai.com/v1",
		"--llm-key", "k",
		"--llm-model", "m",
	)
	if err == nil {
		t.Fatal("expected error, got nil")
	}
	if !strings.Contains(err.Error(), "mutually exclusive") {
		t.Errorf("error = %v", err)
	}
	if hits := atomic.LoadInt32(&srv.hits); hits != 0 {
		t.Errorf("expected 0 HTTP calls, got %d", hits)
	}
}

func TestCallSubcommand_RejectsNeitherMode(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleResponse())

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"call", "+15551234567",
	)
	if err == nil {
		t.Fatal("expected error, got nil")
	}
	if !strings.Contains(err.Error(), "either --prompt or") {
		t.Errorf("error = %v", err)
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

func TestCallSubcommand_PropagatesIdempotencyKey(t *testing.T) {
	t.Run("explicit", func(t *testing.T) {
		srv := newFakeServer(t, http.StatusCreated, sampleResponse())
		_, _, err := runRoot(t,
			map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
			"call", "+15551234567", "--prompt", "hi", "--idempotency-key", "deadbeef-1234",
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
			"call", "+15551234567", "--prompt", "hi",
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
		"call", "+15551234567", "--prompt", "hi",
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
		"--json", "call", "+15551234567", "--prompt", "hi",
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

func TestCallSubcommand_MissingAPIKey(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleResponse())

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_URL": srv.URL}, // no HAIL_API_KEY
		"call", "+15551234567", "--prompt", "hi",
	)
	if err == nil {
		t.Fatal("expected error, got nil")
	}
	if !strings.Contains(err.Error(), "missing API key") {
		t.Errorf("error = %v", err)
	}
	if hits := atomic.LoadInt32(&srv.hits); hits != 0 {
		t.Errorf("expected 0 HTTP calls, got %d", hits)
	}
}
