package cmd

import (
	"encoding/json"
	"net/http"
	"strings"
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
	if srv.lastReq.URL.Path != "/calls/"+resp.Id.String() {
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
