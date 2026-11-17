package cmd

import (
	"encoding/json"
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

func TestCallStatus_HappyPath(t *testing.T) {
	resp := sampleResponse()
	endReason := "hung_up"
	resp.EndReason = &endReason
	endedAt := time.Date(2026, 4, 22, 12, 1, 0, 0, time.UTC)
	resp.EndedAt = &endedAt
	rec := "recordings/abc.wav"
	resp.RecordingS3Key = &rec
	resp.Status = client.CallResponseStatusCompleted

	srv := newFakeServer(t, http.StatusOK, resp)

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"call", "status", resp.Id.String(),
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(stdout, resp.Id.String()) {
		t.Errorf("missing id in stdout: %q", stdout)
	}
	if !strings.Contains(stdout, "completed") {
		t.Errorf("missing status in stdout: %q", stdout)
	}
	if !strings.Contains(stdout, "End reason: hung_up") {
		t.Errorf("missing end_reason in stdout: %q", stdout)
	}
	if !strings.Contains(stdout, "Recording: recordings/abc.wav") {
		t.Errorf("missing recording in stdout: %q", stdout)
	}
	if srv.lastReq.URL.Path != "/v1/calls/"+resp.Id.String() {
		t.Errorf("path = %s", srv.lastReq.URL.Path)
	}
	if srv.lastReq.Method != http.MethodGet {
		t.Errorf("method = %s", srv.lastReq.Method)
	}
}

func TestCallStatus_NotFound(t *testing.T) {
	srv := newFakeServer(t, http.StatusNotFound, map[string]string{"detail": "call not found"})

	id := uuid.NewString()
	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"call", "status", id,
	)
	if err == nil {
		t.Fatal("expected error, got nil")
	}
	if !strings.Contains(err.Error(), "not found") {
		t.Errorf("error = %v", err)
	}
}

func TestCallStatus_JSONOutput(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, sampleResponse())

	id := openapi_types.UUID(uuid.MustParse("11111111-1111-1111-1111-111111111111"))
	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"--json", "call", "status", id.String(),
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var got client.CallResponse
	if err := json.Unmarshal([]byte(stdout), &got); err != nil {
		t.Fatalf("stdout is not valid JSON: %v\n%s", err, stdout)
	}
	if got.Id != id {
		t.Errorf("got.Id = %v", got.Id)
	}
}

func TestCallStatus_PrefixResolution(t *testing.T) {
	fullID := "11111111-1111-1111-1111-111111111111"
	listBody := client.CallListResponse{Items: []client.CallResponse{
		sampleCall(fullID, "+15550001", client.CallResponseStatusCompleted),
		sampleCall("22222222-2222-2222-2222-222222222222", "+15550002", client.CallResponseStatusDialing),
	}}

	detail := sampleCall(fullID, "+15550001", client.CallResponseStatusCompleted)
	detail.Transcript = &[]client.CallTranscriptTurn{
		{Role: client.Assistant, Text: "Hi, this is Acme.", At: time.Date(2026, 4, 22, 12, 0, 1, 0, time.UTC)},
		{Role: client.User, Text: "Hello!", At: time.Date(2026, 4, 22, 12, 0, 3, 0, time.UTC)},
	}
	var hits int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&hits, 1)
		w.Header().Set("Content-Type", "application/json")
		if r.URL.Path == "/v1/calls" {
			_ = json.NewEncoder(w).Encode(listBody)
			return
		}
		if r.URL.Path != "/v1/calls/"+fullID {
			t.Errorf("unexpected path %s", r.URL.Path)
		}
		_ = json.NewEncoder(w).Encode(detail)
	}))
	t.Cleanup(srv.Close)

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"call", "status", "11111111",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	// The list resolves the prefix; the GET by id brings the transcript.
	if got := atomic.LoadInt32(&hits); got != 2 {
		t.Errorf("expected 2 requests (list, then GET by id), got %d", got)
	}
	if !strings.Contains(stdout, fullID) {
		t.Errorf("stdout missing resolved UUID:\n%s", stdout)
	}
	if !strings.Contains(stdout, "Agent: Hi, this is Acme.") || !strings.Contains(stdout, "Person: Hello!") {
		t.Errorf("stdout missing transcript:\n%s", stdout)
	}
}

func TestCallStatus_PrefixAmbiguous(t *testing.T) {
	listBody := client.CallListResponse{Items: []client.CallResponse{
		sampleCall("11111111-1111-1111-1111-111111111111", "+15550001", client.CallResponseStatusCompleted),
		sampleCall("11111111-2222-3333-4444-555555555555", "+15550002", client.CallResponseStatusDialing),
	}}

	srv := newFakeServer(t, http.StatusOK, listBody)

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"call", "status", "11111111",
	)
	if err == nil {
		t.Fatal("expected ambiguous error")
	}
	if !strings.Contains(err.Error(), "ambiguous") {
		t.Errorf("error = %v; expected ambiguous", err)
	}
}

func TestCallStatus_PrefixNoMatch(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, client.CallListResponse{Items: nil})

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"call", "status", "deadbeef",
	)
	if err == nil {
		t.Fatal("expected no-match error")
	}
	if !strings.Contains(err.Error(), "no call matches") {
		t.Errorf("error = %v", err)
	}
}

func TestCallStatus_RejectsBadShape(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, sampleResponse())

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"call", "status", "xyz", // not hex, too short
	)
	if err == nil {
		t.Fatal("expected error")
	}
	if !strings.Contains(err.Error(), "invalid call id") {
		t.Errorf("error = %v", err)
	}
	if hits := atomic.LoadInt32(&srv.hits); hits != 0 {
		t.Errorf("expected 0 HTTP calls for malformed input, got %d", hits)
	}
}
