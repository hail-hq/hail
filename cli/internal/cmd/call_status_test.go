package cmd

import (
	"encoding/json"
	"net/http"
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

	srv := newFakeServer(t, http.StatusOK, listBody)

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"call", "status", "11111111",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	// Single round trip — the matched record is reused from the list response.
	if got := atomic.LoadInt32(&srv.hits); got != 1 {
		t.Errorf("expected 1 request (list only, no follow-up GET), got %d", got)
	}
	if srv.lastReq.URL.Path != "/v1/calls" {
		t.Errorf("path = %s; want /v1/calls", srv.lastReq.URL.Path)
	}
	if !strings.Contains(stdout, fullID) {
		t.Errorf("stdout missing resolved UUID:\n%s", stdout)
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
