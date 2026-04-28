package cmd

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	"github.com/google/uuid"
	openapi_types "github.com/oapi-codegen/runtime/types"

	"github.com/hail-hq/hail/cli/internal/client"
)

// callA / callB pin the call ids the org-wide tail tests assign so the
// expected short-id prefixes are deterministic.
var (
	callA = openapi_types.UUID(uuid.MustParse("c2a8f1d3-1111-1111-1111-111111111111"))
	callB = openapi_types.UUID(uuid.MustParse("d4e9b2c5-2222-2222-2222-222222222222"))
)

func sampleEventInCall(idStr string, callID openapi_types.UUID, kind string, payload map[string]interface{}, ts time.Time) client.CallEventResponse {
	return client.CallEventResponse{
		Id:         openapi_types.UUID(uuid.MustParse(idStr)),
		CallId:     callID,
		Kind:       kind,
		Payload:    payload,
		OccurredAt: ts,
	}
}

// completedStatus / dialingStatus are pointer helpers (the new schema makes
// EventStreamResponse.CallStatus a *pointer* — only set when the request
// narrows to a single call).
func completedStatus() *client.EventStreamResponseCallStatus {
	s := client.EventStreamResponseCallStatusCompleted
	return &s
}
func dialingStatus() *client.EventStreamResponseCallStatus {
	s := client.EventStreamResponseCallStatusDialing
	return &s
}

// TestTail_HappyPath_OrgWide: org-wide tail prints events from two calls,
// each prefixed with a short id; the loop runs until SIGINT.
func TestTail_HappyPath_OrgWide(t *testing.T) {
	t0 := time.Now().Add(time.Hour) // future-stamped so default "from now" still includes them
	events1 := []client.CallEventResponse{
		sampleEventInCall("11111111-1111-1111-1111-111111111111", callA, "state_change",
			map[string]interface{}{"from": "queued", "to": "dialing"}, t0),
		sampleEventInCall("22222222-2222-2222-2222-222222222221", callA, "agent_turn",
			map[string]interface{}{"text": "Hi from A."}, t0.Add(time.Second)),
	}
	events2 := []client.CallEventResponse{
		sampleEventInCall("33333333-3333-3333-3333-333333333331", callB, "state_change",
			map[string]interface{}{"from": "queued", "to": "dialing"}, t0.Add(2*time.Second)),
		sampleEventInCall("44444444-4444-4444-4444-444444444441", callB, "agent_turn",
			map[string]interface{}{"text": "Hi from B."}, t0.Add(3*time.Second)),
	}

	// Custom handler: poll 1 returns events1; poll 2 returns events2; poll 3+
	// returns empty + sends SIGINT to terminate the loop.
	var hits int32
	var lastReq *http.Request
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n := atomic.AddInt32(&hits, 1)
		lastReq = r.Clone(r.Context())
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		switch n {
		case 1:
			_ = json.NewEncoder(w).Encode(client.EventStreamResponse{Items: events1})
		case 2:
			_ = json.NewEncoder(w).Encode(client.EventStreamResponse{Items: events2})
		default:
			_ = json.NewEncoder(w).Encode(client.EventStreamResponse{Items: []client.CallEventResponse{}})
			// Trigger SIGINT after the third response so the tail loop's
			// signal handler returns errInterrupted.
			go func() {
				time.Sleep(20 * time.Millisecond)
				_ = syscall.Kill(syscall.Getpid(), syscall.SIGINT)
			}()
		}
	}))
	t.Cleanup(srv.Close)

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL, "NO_COLOR": "1"},
		"tail", "--interval", "100", "--from-start",
	)
	if err != nil && !errors.Is(err, errInterrupted) {
		t.Fatalf("unexpected error: %v", err)
	}
	if got := atomic.LoadInt32(&hits); got < 2 {
		t.Errorf("expected at least 2 polls, got %d", got)
	}
	if lastReq == nil || lastReq.URL.Path != "/events" {
		t.Errorf("expected request path /events, got %v", lastReq)
	}
	for _, want := range []string{
		"queued → dialing",
		"Hi from A.",
		"Hi from B.",
		"[c2a8f1d3]", // short id of callA
		"[d4e9b2c5]", // short id of callB
	} {
		if !strings.Contains(stdout, want) {
			t.Errorf("stdout missing %q\n%s", want, stdout)
		}
	}
}

// TestTail_WithIdFlagFiltersAndAutoExits: --id call:<uuid> narrows to one
// call, puts id=call:<uuid> on the wire, and exits when call_status reaches
// a terminal.
func TestTail_WithIdFlagFiltersAndAutoExits(t *testing.T) {
	t0 := time.Now().Add(-time.Minute) // past-stamped is fine: the test uses --from-start below
	srv := newSequenceServer(t, []sequenceResponse{
		{http.StatusOK, client.EventStreamResponse{
			Items: []client.CallEventResponse{
				sampleEventInCall("11111111-1111-1111-1111-111111111111", callA, "agent_turn",
					map[string]interface{}{"text": "hello"}, t0),
			},
			CallStatus: completedStatus(),
		}},
	})

	idValue := "call:" + uuid.UUID(callA).String()
	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL, "NO_COLOR": "1"},
		"tail", "--id", idValue, "--from-start",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	q := srv.lastReq.URL.Query()
	if got := q.Get("id"); got != idValue {
		t.Errorf("expected id query=%s, got %q", idValue, got)
	}
	if got := q.Get("call_id"); got != "" {
		t.Errorf("legacy call_id should not appear, got %q", got)
	}
	if !strings.Contains(stdout, "call completed") {
		t.Errorf("stdout missing terminal-status line:\n%s", stdout)
	}
	if !strings.Contains(stdout, "hello") {
		t.Errorf("stdout missing event body:\n%s", stdout)
	}
}

// TestTail_RejectsMalformedId: a malformed --id value fails fast with a
// helpful error and makes no HTTP call.
func TestTail_RejectsMalformedId(t *testing.T) {
	cases := []struct {
		name      string
		id        string
		mustMatch string
	}{
		{"missing colon", "badvalue", "missing ':'"},
		{"bad uuid", "call:notuuid", "invalid uuid"},
		{"bare colon", ":", "missing resource type"},
		{"empty id", "call:", "missing resource id"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			srv := newFakeServer(t, http.StatusOK, client.EventStreamResponse{})
			_, _, err := runRoot(t,
				map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL, "NO_COLOR": "1"},
				"tail", "--id", tc.id,
			)
			if err == nil {
				t.Fatal("expected error, got nil")
			}
			if !strings.Contains(err.Error(), tc.mustMatch) {
				t.Errorf("error %q missing %q", err.Error(), tc.mustMatch)
			}
			if hits := atomic.LoadInt32(&srv.hits); hits != 0 {
				t.Errorf("expected 0 HTTP calls, got %d", hits)
			}
		})
	}
}

// TestTail_RejectsUnsupportedType: --id sms:<uuid> fails fast on the CLI
// before any HTTP, with the supported-types message.
func TestTail_RejectsUnsupportedType(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, client.EventStreamResponse{})
	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL, "NO_COLOR": "1"},
		"tail", "--id", "sms:"+uuid.UUID(callA).String(),
	)
	if err == nil {
		t.Fatal("expected error, got nil")
	}
	msg := err.Error()
	if !strings.Contains(msg, "unsupported resource type") {
		t.Errorf("error %q missing 'unsupported resource type'", msg)
	}
	if !strings.Contains(msg, "\"sms\"") {
		t.Errorf("error %q missing the offending type token", msg)
	}
	if !strings.Contains(msg, "supported: call") {
		t.Errorf("error %q missing supported-types list", msg)
	}
	if hits := atomic.LoadInt32(&srv.hits); hits != 0 {
		t.Errorf("expected 0 HTTP calls, got %d", hits)
	}
}

// TestTail_NoFollowOneShot: --no-follow does one fetch and exits, even with
// non-terminal status.
func TestTail_NoFollowOneShot(t *testing.T) {
	srv := newSequenceServer(t, []sequenceResponse{
		{http.StatusOK, client.EventStreamResponse{
			Items:      []client.CallEventResponse{},
			CallStatus: dialingStatus(),
		}},
	})

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL, "NO_COLOR": "1"},
		"tail", "--no-follow",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got := atomic.LoadInt32(&srv.hits); got != 1 {
		t.Errorf("expected exactly 1 poll, got %d", got)
	}
}

// TestTail_FromStartFetchesHistorical: --from-start omits the cursor on the
// first request and includes events with timestamps in the past.
func TestTail_FromStartFetchesHistorical(t *testing.T) {
	tPast := time.Date(2026, 4, 22, 12, 0, 0, 0, time.UTC) // well before time.Now()
	srv := newSequenceServer(t, []sequenceResponse{
		{http.StatusOK, client.EventStreamResponse{
			Items: []client.CallEventResponse{
				sampleEventInCall("11111111-1111-1111-1111-111111111111", callA, "agent_turn",
					map[string]interface{}{"text": "ancient"}, tPast),
			},
			CallStatus: completedStatus(),
		}},
	})

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL, "NO_COLOR": "1"},
		"tail", "--id", "call:"+uuid.UUID(callA).String(), "--from-start",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cur := srv.lastReq.URL.Query().Get("cursor"); cur != "" {
		t.Errorf("--from-start should omit cursor on first request, got %q", cur)
	}
	if !strings.Contains(stdout, "ancient") {
		t.Errorf("--from-start should print historical events:\n%s", stdout)
	}
}

// TestTail_KindFilter: --kind agent_turn puts kind=agent_turn on the wire.
func TestTail_KindFilter(t *testing.T) {
	srv := newSequenceServer(t, []sequenceResponse{
		{http.StatusOK, client.EventStreamResponse{
			Items:      []client.CallEventResponse{},
			CallStatus: completedStatus(),
		}},
	})

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL, "NO_COLOR": "1"},
		"tail", "--id", "call:"+uuid.UUID(callA).String(), "--kind", "agent_turn",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if k := srv.lastReq.URL.Query().Get("kind"); k != "agent_turn" {
		t.Errorf("expected kind=agent_turn on wire, got %q", k)
	}
}

// TestTail_DefensiveOnUnknownKind: the CLI prints best-effort output for
// unknown kinds and missing fields without crashing.
func TestTail_DefensiveOnUnknownKind(t *testing.T) {
	tFuture := time.Now().Add(time.Hour)
	srv := newSequenceServer(t, []sequenceResponse{
		{http.StatusOK, client.EventStreamResponse{
			Items: []client.CallEventResponse{
				sampleEventInCall("11111111-1111-1111-1111-111111111111", callA, "weird_event",
					map[string]interface{}{"a": 1, "b": "two"}, tFuture),
				sampleEventInCall("22222222-2222-2222-2222-222222222221", callA, "agent_turn",
					map[string]interface{}{"role": "assistant"}, tFuture.Add(time.Second)),
			},
			CallStatus: completedStatus(),
		}},
	})

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL, "NO_COLOR": "1"},
		"tail", "--id", "call:"+uuid.UUID(callA).String(),
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(stdout, "[weird_event]") {
		t.Errorf("missing [weird_event] label:\n%s", stdout)
	}
	if !strings.Contains(stdout, "\"a\":1") && !strings.Contains(stdout, "\"a\": 1") {
		t.Errorf("missing payload JSON for unknown kind:\n%s", stdout)
	}
	if !strings.Contains(stdout, "[agent]") {
		t.Errorf("missing [agent] label for fallback:\n%s", stdout)
	}
}

// TestTail_NDJSONOutput: --json mode emits one JSON object per line and
// suppresses the synthetic terminal-status line.
func TestTail_NDJSONOutput(t *testing.T) {
	tFuture := time.Now().Add(time.Hour)
	srv := newSequenceServer(t, []sequenceResponse{
		{http.StatusOK, client.EventStreamResponse{
			Items: []client.CallEventResponse{
				sampleEventInCall("11111111-1111-1111-1111-111111111111", callA, "agent_turn",
					map[string]interface{}{"text": "hi"}, tFuture),
				sampleEventInCall("22222222-2222-2222-2222-222222222221", callA, "user_turn",
					map[string]interface{}{"text": "hello"}, tFuture.Add(time.Second)),
			},
			CallStatus: completedStatus(),
		}},
	})

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL, "NO_COLOR": "1"},
		"--json", "tail", "--id", "call:"+uuid.UUID(callA).String(),
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	lines := strings.Split(strings.TrimRight(stdout, "\n"), "\n")
	if len(lines) != 2 {
		t.Fatalf("expected exactly 2 NDJSON lines (no synthetic terminal line), got %d:\n%s", len(lines), stdout)
	}
	for i, line := range lines {
		var anyObj map[string]any
		if err := json.Unmarshal([]byte(line), &anyObj); err != nil {
			t.Errorf("line %d not valid JSON: %v\n%s", i, err, line)
		}
		// Each emitted record must be an actual event (has an `id`), not a
		// synthetic system marker.
		if _, ok := anyObj["id"]; !ok {
			t.Errorf("line %d missing event id field — looks synthetic:\n%s", i, line)
		}
	}
}

// TestTail_PrependsShortCallIdInOrgMode: org-wide mode (no --id) prepends
// the first 8 chars of the UUID between the timestamp and the kind label.
func TestTail_PrependsShortCallIdInOrgMode(t *testing.T) {
	tFuture := time.Now().Add(time.Hour)
	// Set up two responses: one with events, then signal SIGINT to exit.
	var hits int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n := atomic.AddInt32(&hits, 1)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		if n == 1 {
			_ = json.NewEncoder(w).Encode(client.EventStreamResponse{
				Items: []client.CallEventResponse{
					sampleEventInCall("11111111-1111-1111-1111-111111111111", callA, "agent_turn",
						map[string]interface{}{"text": "hi A"}, tFuture),
					sampleEventInCall("22222222-2222-2222-2222-222222222221", callB, "agent_turn",
						map[string]interface{}{"text": "hi B"}, tFuture.Add(time.Second)),
				},
			})
		} else {
			_ = json.NewEncoder(w).Encode(client.EventStreamResponse{Items: []client.CallEventResponse{}})
			go func() {
				time.Sleep(20 * time.Millisecond)
				_ = syscall.Kill(syscall.Getpid(), syscall.SIGINT)
			}()
		}
	}))
	t.Cleanup(srv.Close)

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL, "NO_COLOR": "1"},
		"tail", "--interval", "100", "--from-start",
	)
	if err != nil && !errors.Is(err, errInterrupted) {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(stdout, "[c2a8f1d3]") {
		t.Errorf("missing [c2a8f1d3] short id prefix:\n%s", stdout)
	}
	if !strings.Contains(stdout, "[d4e9b2c5]") {
		t.Errorf("missing [d4e9b2c5] short id prefix:\n%s", stdout)
	}
}

// TestTail_OmitsShortCallIdWhenIdFlagSet: --id call:<uuid> mode does NOT
// prepend the short id (every event belongs to the same call).
func TestTail_OmitsShortCallIdWhenIdFlagSet(t *testing.T) {
	tFuture := time.Now().Add(time.Hour)
	srv := newSequenceServer(t, []sequenceResponse{
		{http.StatusOK, client.EventStreamResponse{
			Items: []client.CallEventResponse{
				sampleEventInCall("11111111-1111-1111-1111-111111111111", callA, "agent_turn",
					map[string]interface{}{"text": "hi"}, tFuture),
			},
			CallStatus: completedStatus(),
		}},
	})

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL, "NO_COLOR": "1"},
		"tail", "--id", "call:"+uuid.UUID(callA).String(),
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if strings.Contains(stdout, "[c2a8f1d3]") {
		t.Errorf("--id call:<uuid> mode should not include short id prefix:\n%s", stdout)
	}
}

// TestTail_PropagatesCursorAcrossPolls: the third request's cursor matches
// the cursor synthesized from poll 2's last event (same wire encoding as
// the API uses).
func TestTail_PropagatesCursorAcrossPolls(t *testing.T) {
	tFuture := time.Now().Add(time.Hour)
	last2 := sampleEventInCall("22222222-2222-2222-2222-222222222221", callA, "agent_turn",
		map[string]interface{}{"text": "second-poll-last"}, tFuture.Add(2*time.Second))
	srv := newSequenceServer(t, []sequenceResponse{
		{http.StatusOK, client.EventStreamResponse{
			Items: []client.CallEventResponse{
				sampleEventInCall("11111111-1111-1111-1111-111111111111", callA, "agent_turn",
					map[string]interface{}{"text": "first"}, tFuture),
			},
			CallStatus: dialingStatus(),
		}},
		{http.StatusOK, client.EventStreamResponse{
			Items:      []client.CallEventResponse{last2},
			CallStatus: dialingStatus(),
		}},
		// Poll 3: empty + completed → loop exits.
		{http.StatusOK, client.EventStreamResponse{
			Items:      []client.CallEventResponse{},
			CallStatus: completedStatus(),
		}},
	})

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL, "NO_COLOR": "1"},
		"tail", "--id", "call:"+uuid.UUID(callA).String(), "--from-start", "--interval", "100",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if hits := atomic.LoadInt32(&srv.hits); hits < 3 {
		t.Fatalf("expected at least 3 polls, got %d", hits)
	}
	wantCursor := encodeEventCursor(last2.OccurredAt, last2.Id)
	gotCursor := srv.lastReq.URL.Query().Get("cursor")
	if gotCursor != wantCursor {
		t.Errorf("third poll cursor mismatch:\n want=%s\n  got=%s", wantCursor, gotCursor)
	}
}
