package cmd

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"regexp"
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
	callA  = openapi_types.UUID(uuid.MustParse("c2a8f1d3-1111-1111-1111-111111111111"))
	callB  = openapi_types.UUID(uuid.MustParse("d4e9b2c5-2222-2222-2222-222222222222"))
	emailA = openapi_types.UUID(uuid.MustParse("e5f0c3d6-3333-3333-3333-333333333333"))
)

func sampleEventInCall(idStr string, callID openapi_types.UUID, kind string, payload map[string]interface{}, ts time.Time) client.EventResponse {
	return client.EventResponse{
		Id:         openapi_types.UUID(uuid.MustParse(idStr)),
		CallId:     &callID,
		Kind:       kind,
		Payload:    payload,
		OccurredAt: ts,
	}
}

func sampleEventInEmail(idStr string, emailID openapi_types.UUID, kind string, payload map[string]interface{}, ts time.Time) client.EventResponse {
	return client.EventResponse{
		Id:         openapi_types.UUID(uuid.MustParse(idStr)),
		EmailId:    &emailID,
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
	events1 := []client.EventResponse{
		sampleEventInCall("11111111-1111-1111-1111-111111111111", callA, "state_change",
			map[string]interface{}{"from": "queued", "to": "dialing"}, t0),
		sampleEventInCall("22222222-2222-2222-2222-222222222221", callA, "agent_turn",
			map[string]interface{}{"text": "Hi from A."}, t0.Add(time.Second)),
	}
	events2 := []client.EventResponse{
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
			_ = json.NewEncoder(w).Encode(client.EventStreamResponse{Items: []client.EventResponse{}})
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
			Items: []client.EventResponse{
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
		// A bare value defaults to a call id; non-hex garbage fails the
		// local shape check before any HTTP.
		{"bare non-id", "badvalue", "invalid call id"},
		{"bad uuid", "call:notuuid", "invalid call id"},
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

// TestTail_RejectsUnsupportedType: --id fax:<uuid> fails fast on the CLI
// before any HTTP, with the supported-types message.
func TestTail_RejectsUnsupportedType(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, client.EventStreamResponse{})
	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL, "NO_COLOR": "1"},
		"tail", "--id", "fax:"+uuid.UUID(callA).String(),
	)
	if err == nil {
		t.Fatal("expected error, got nil")
	}
	msg := err.Error()
	if !strings.Contains(msg, "unsupported resource type") {
		t.Errorf("error %q missing 'unsupported resource type'", msg)
	}
	if !strings.Contains(msg, "\"fax\"") {
		t.Errorf("error %q missing the offending type token", msg)
	}
	if !strings.Contains(msg, "supported: call, email, sms") {
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
			Items:      []client.EventResponse{},
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
			Items: []client.EventResponse{
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
			Items:      []client.EventResponse{},
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
			Items: []client.EventResponse{
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
	if !strings.Contains(stdout, "[hail]") {
		t.Errorf("missing [hail] label for fallback:\n%s", stdout)
	}
}

// TestTail_NDJSONOutput: --json mode emits one JSON object per line and
// suppresses the synthetic terminal-status line.
func TestTail_NDJSONOutput(t *testing.T) {
	tFuture := time.Now().Add(time.Hour)
	srv := newSequenceServer(t, []sequenceResponse{
		{http.StatusOK, client.EventStreamResponse{
			Items: []client.EventResponse{
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
				Items: []client.EventResponse{
					sampleEventInCall("11111111-1111-1111-1111-111111111111", callA, "agent_turn",
						map[string]interface{}{"text": "hi A"}, tFuture),
					sampleEventInCall("22222222-2222-2222-2222-222222222221", callB, "agent_turn",
						map[string]interface{}{"text": "hi B"}, tFuture.Add(time.Second)),
				},
			})
		} else {
			_ = json.NewEncoder(w).Encode(client.EventStreamResponse{Items: []client.EventResponse{}})
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

// TestTail_RendersEmailSourceEvent: org-wide tail renders events from email
// sources (EmailId set, CallId nil), prefixing with the email's short id.
func TestTail_RendersEmailSourceEvent(t *testing.T) {
	tFuture := time.Now().Add(time.Hour)
	// Set up two responses: one with an email event, then signal SIGINT to exit.
	var hits int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n := atomic.AddInt32(&hits, 1)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		if n == 1 {
			_ = json.NewEncoder(w).Encode(client.EventStreamResponse{
				Items: []client.EventResponse{
					sampleEventInEmail("55555555-5555-5555-5555-555555555555", emailA, "sent",
						map[string]interface{}{"to": "recipient@example.com", "subject": "Test"}, tFuture),
				},
			})
		} else {
			_ = json.NewEncoder(w).Encode(client.EventStreamResponse{Items: []client.EventResponse{}})
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
	if !strings.Contains(stdout, "[e5f0c3d6]") {
		t.Errorf("missing [e5f0c3d6] short id prefix for email:\n%s", stdout)
	}
	if !strings.Contains(stdout, "[sent]") {
		t.Errorf("missing [sent] kind label:\n%s", stdout)
	}
}

// TestTail_OmitsShortCallIdWhenIdFlagSet: --id call:<uuid> mode does NOT
// prepend the short id (every event belongs to the same call).
func TestTail_OmitsShortCallIdWhenIdFlagSet(t *testing.T) {
	tFuture := time.Now().Add(time.Hour)
	srv := newSequenceServer(t, []sequenceResponse{
		{http.StatusOK, client.EventStreamResponse{
			Items: []client.EventResponse{
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
			Items: []client.EventResponse{
				sampleEventInCall("11111111-1111-1111-1111-111111111111", callA, "agent_turn",
					map[string]interface{}{"text": "first"}, tFuture),
			},
			CallStatus: dialingStatus(),
		}},
		{http.StatusOK, client.EventStreamResponse{
			Items:      []client.EventResponse{last2},
			CallStatus: dialingStatus(),
		}},
		// Poll 3: empty + completed → loop exits.
		{http.StatusOK, client.EventStreamResponse{
			Items:      []client.EventResponse{},
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

func TestTail_PositionalCall_Equiv_FlagID(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, map[string]any{"items": []any{}, "next_cursor": nil})
	id := "11111111-1111-1111-1111-111111111111"

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"tail", "call:"+id, "--no-follow",
	)
	if err != nil {
		t.Fatalf("positional: %v", err)
	}
	q := srv.lastReq.URL.Query().Get("id")
	if q != "call:"+id {
		t.Fatalf("expected id query, got %q", q)
	}
}

func TestTail_PositionalEmail(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, map[string]any{"items": []any{}, "next_cursor": nil})
	id := "22222222-2222-2222-2222-222222222222"
	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"tail", "email:"+id, "--no-follow",
	)
	if err != nil {
		t.Fatalf("email positional: %v", err)
	}
	q := srv.lastReq.URL.Query().Get("id")
	if q != "email:"+id {
		t.Fatalf("expected email id query, got %q", q)
	}
}

func TestTail_PositionalAndFlagDisagree(t *testing.T) {
	a := "11111111-1111-1111-1111-111111111111"
	b := "22222222-2222-2222-2222-222222222222"
	_, stderr, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test"},
		"tail", "call:"+a, "--id", "call:"+b, "--no-follow",
	)
	if !errors.Is(err, errInvalidInputs) {
		t.Fatalf("want errInvalidInputs, got %v", err)
	}
	if !strings.Contains(stderr, "--id and positional disagree") {
		t.Fatalf("missing reason: %q", stderr)
	}
}

func TestTail_UnsupportedType(t *testing.T) {
	id := "11111111-1111-1111-1111-111111111111"
	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test"},
		"tail", "fax:"+id, "--no-follow",
	)
	if err == nil || !strings.Contains(err.Error(), "unsupported resource type") {
		t.Fatalf("want unsupported-type rejection, got %v", err)
	}
}

// TestTail_MachineTranscriptsRelabeled: pickup transcripts that precede a
// machine AMD verdict render as [machine], never [human]; the amd_result
// row renders as a compact [amd] verdict, not payload JSON.
func TestTail_MachineTranscriptsRelabeled(t *testing.T) {
	tFuture := time.Now().Add(time.Hour)
	srv := newSequenceServer(t, []sequenceResponse{
		{http.StatusOK, client.EventStreamResponse{
			Items: []client.EventResponse{
				sampleEventInCall("11111111-1111-1111-1111-111111111111", callA, "user_turn",
					map[string]interface{}{"text": "press one."}, tFuture),
				sampleEventInCall("22222222-2222-2222-2222-222222222221", callA, "amd_result",
					map[string]interface{}{"category": "machine-ivr", "transcript": "press one."}, tFuture.Add(time.Second)),
				sampleEventInCall("33333333-3333-3333-3333-333333333331", callA, "agent_turn",
					map[string]interface{}{"text": "Hi, this is an AI assistant."}, tFuture.Add(2*time.Second)),
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
	if !strings.Contains(stdout, "[machine]") || !strings.Contains(stdout, "press one.") {
		t.Errorf("machine transcript not relabeled:\n%s", stdout)
	}
	if strings.Contains(stdout, "[human]") || strings.Contains(stdout, "[user]") {
		t.Errorf("machine speech leaked a human/user label:\n%s", stdout)
	}
	if !strings.Contains(stdout, "[amd]") || !strings.Contains(stdout, "answered by a machine (phone menu)") {
		t.Errorf("missing [amd] verdict sentence:\n%s", stdout)
	}
	if strings.Contains(stdout, "transcript") {
		t.Errorf("amd_result should not dump payload JSON:\n%s", stdout)
	}
	if !strings.Contains(stdout, "[hail]") {
		t.Errorf("agent line should render as [hail]:\n%s", stdout)
	}
}

// TestTail_HumanPickupFlushesAsHuman: transcripts buffered during AMD
// flush as [human] when the verdict is human, preserving order before the
// verdict line.
func TestTail_HumanPickupFlushesAsHuman(t *testing.T) {
	tFuture := time.Now().Add(time.Hour)
	srv := newSequenceServer(t, []sequenceResponse{
		{http.StatusOK, client.EventStreamResponse{
			Items: []client.EventResponse{
				sampleEventInCall("11111111-1111-1111-1111-111111111111", callA, "user_turn",
					map[string]interface{}{"text": "Hello?"}, tFuture),
				sampleEventInCall("22222222-2222-2222-2222-222222222221", callA, "amd_result",
					map[string]interface{}{"category": "human"}, tFuture.Add(time.Second)),
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
	if !strings.Contains(stdout, "[human]") || !strings.Contains(stdout, "Hello?") {
		t.Errorf("human pickup transcript missing:\n%s", stdout)
	}
	if strings.Index(stdout, "Hello?") > strings.Index(stdout, "[amd]") {
		t.Errorf("buffered transcript must print before the verdict line:\n%s", stdout)
	}
}

// TestTail_SilenceDotsBetweenEvents: a short gap prints one dim "." line
// per second; a long gap compresses to three dots plus a duration.
func TestTail_SilenceDotsBetweenEvents(t *testing.T) {
	tFuture := time.Now().Add(time.Hour)
	srv := newSequenceServer(t, []sequenceResponse{
		{http.StatusOK, client.EventStreamResponse{
			Items: []client.EventResponse{
				sampleEventInCall("11111111-1111-1111-1111-111111111111", callA, "agent_turn",
					map[string]interface{}{"text": "One moment."}, tFuture),
				sampleEventInCall("22222222-2222-2222-2222-222222222221", callA, "agent_turn",
					map[string]interface{}{"text": "Still here."}, tFuture.Add(5*time.Second)),
				sampleEventInCall("33333333-3333-3333-3333-333333333331", callA, "agent_turn",
					map[string]interface{}{"text": "Back again."}, tFuture.Add(3605*time.Second)),
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
	dotLines := 0
	for _, line := range strings.Split(stdout, "\n") {
		if strings.Contains(line, "[wait]") && strings.HasSuffix(line, " .") {
			dotLines++
		}
	}
	// 5 for the 5s gap + 3 for the compressed hour-long gap.
	if dotLines != 8 {
		t.Errorf("dot lines = %d, want 8:\n%s", dotLines, stdout)
	}
	// Dot lines carry the standard timestamp prefix: "[HH:MM:SS] [wait]    ."
	if !regexp.MustCompile(`(?m)^\[\d{2}:\d{2}:\d{2}\] \[wait\]\s+\.$`).MatchString(stdout) {
		t.Errorf("dot lines missing timestamped [wait] prefix:\n%s", stdout)
	}
	if !strings.Contains(stdout, "[wait]") || !strings.Contains(stdout, "1h0m0s silent") {
		t.Errorf("missing compressed silence duration:\n%s", stdout)
	}
}

// TestTail_BareUUIDDefaultsToCall: --id with a bare UUID (no "call:"
// prefix) narrows to that call.
func TestTail_BareUUIDDefaultsToCall(t *testing.T) {
	srv := newSequenceServer(t, []sequenceResponse{
		{http.StatusOK, client.EventStreamResponse{CallStatus: completedStatus()}},
	})

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL, "NO_COLOR": "1"},
		"tail", "--id", uuid.UUID(callA).String(),
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	got := srv.lastReq.URL.Query().Get("id")
	want := "call:" + uuid.UUID(callA).String()
	if got != want {
		t.Errorf("wire id = %q, want %q", got, want)
	}
}

// TestTail_ShortPrefixResolvesViaCallsList: --id call:<prefix> resolves the
// full UUID through GET /calls before polling events.
func TestTail_ShortPrefixResolvesViaCallsList(t *testing.T) {
	var eventsQuery atomic.Value
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		switch {
		case strings.HasPrefix(r.URL.Path, "/calls"):
			_ = json.NewEncoder(w).Encode(client.CallListResponse{
				Items: []client.CallResponse{sampleCall(uuid.UUID(callA).String(), "+15551234567", client.CallResponseStatusCompleted)},
			})
		default:
			eventsQuery.Store(r.URL.Query().Get("id"))
			_ = json.NewEncoder(w).Encode(client.EventStreamResponse{CallStatus: completedStatus()})
		}
	}))
	defer srv.Close()

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL, "NO_COLOR": "1"},
		"tail", "--id", "call:c2a8f1d3",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	want := "call:" + uuid.UUID(callA).String()
	if got, _ := eventsQuery.Load().(string); got != want {
		t.Errorf("events wire id = %q, want %q", got, want)
	}
}
