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

// newSequenceServer returns a fakeServer that yields responses[i] for the
// i-th hit. Once exhausted, the last response is repeated.
func newSequenceServer(t *testing.T, responses []sequenceResponse) *fakeServer {
	t.Helper()
	fs := &fakeServer{}
	fs.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		idx := int(atomic.AddInt32(&fs.hits, 1)) - 1
		body, _ := io.ReadAll(r.Body)
		fs.lastReq = r.Clone(r.Context())
		fs.lastBody = body
		i := idx
		if i >= len(responses) {
			i = len(responses) - 1
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(responses[i].status)
		_ = json.NewEncoder(w).Encode(responses[i].body)
	}))
	t.Cleanup(fs.Close)
	return fs
}

type sequenceResponse struct {
	status int
	body   any
}

func sampleCall(idStr, to string, status client.CallResponseStatus) client.CallResponse {
	id := openapi_types.UUID(uuid.MustParse(idStr))
	now := time.Date(2026, 4, 22, 12, 0, 0, 0, time.UTC)
	orgID := openapi_types.UUID(uuid.MustParse("22222222-2222-2222-2222-222222222222"))
	return client.CallResponse{
		Id:             id,
		OrganizationId: orgID,
		FromE164:       "+14155551234",
		ToE164:         to,
		Direction:      client.Outbound,
		Status:         status,
		RequestedAt:    now,
	}
}

func TestCallList_RendersTable(t *testing.T) {
	calls := []client.CallResponse{
		sampleCall("11111111-1111-1111-1111-111111111111", "+15551110001", client.CallResponseStatusCompleted),
		sampleCall("22222222-2222-2222-2222-222222222221", "+15551110002", client.CallResponseStatusDialing),
		sampleCall("33333333-3333-3333-3333-333333333331", "+15551110003", client.CallResponseStatusFailed),
	}
	srv := newFakeServer(t, http.StatusOK, client.CallListResponse{Items: calls})

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"call", "list",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(stdout, "ID") || !strings.Contains(stdout, "TO") || !strings.Contains(stdout, "STATUS") || !strings.Contains(stdout, "REQUESTED") {
		t.Errorf("missing header columns in stdout:\n%s", stdout)
	}
	// Truncated 8-char id.
	if !strings.Contains(stdout, "11111111") {
		t.Errorf("missing first id prefix")
	}
	if !strings.Contains(stdout, "+15551110001") || !strings.Contains(stdout, "+15551110003") {
		t.Errorf("missing to numbers in stdout:\n%s", stdout)
	}
	if !strings.Contains(stdout, "completed") || !strings.Contains(stdout, "dialing") || !strings.Contains(stdout, "failed") {
		t.Errorf("missing statuses in stdout:\n%s", stdout)
	}
	// Full UUID should NOT be in human output.
	if strings.Contains(stdout, "11111111-1111-1111-1111-111111111111") {
		t.Errorf("human output should truncate uuid; got %q", stdout)
	}
}

func TestCallList_PaginationWalkAll(t *testing.T) {
	page1Cursor := "cursor-1"
	page2Cursor := "cursor-2"
	srv := newSequenceServer(t, []sequenceResponse{
		{http.StatusOK, client.CallListResponse{
			Items:      []client.CallResponse{sampleCall("11111111-1111-1111-1111-111111111111", "+15550001", client.CallResponseStatusCompleted)},
			NextCursor: &page1Cursor,
		}},
		{http.StatusOK, client.CallListResponse{
			Items:      []client.CallResponse{sampleCall("22222222-2222-2222-2222-222222222221", "+15550002", client.CallResponseStatusCompleted)},
			NextCursor: &page2Cursor,
		}},
		{http.StatusOK, client.CallListResponse{
			Items: []client.CallResponse{sampleCall("33333333-3333-3333-3333-333333333331", "+15550003", client.CallResponseStatusCompleted)},
		}},
	})

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"call", "list", "--all",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got := atomic.LoadInt32(&srv.hits); got != 3 {
		t.Errorf("expected 3 HTTP calls, got %d", got)
	}
	for _, prefix := range []string{"11111111", "22222222", "33333333"} {
		if !strings.Contains(stdout, prefix) {
			t.Errorf("missing %s in stdout:\n%s", prefix, stdout)
		}
	}
}

func TestCallList_StatusFilter(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, client.CallListResponse{Items: nil})

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"call", "list", "--status", "dialing",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got := srv.lastReq.URL.Query().Get("status"); got != "dialing" {
		t.Errorf("?status = %q, want dialing", got)
	}
}
