package cmd

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
)

func TestEmailEventsRendersTimeline(t *testing.T) {
	srv := &fakeServer{}
	srv.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&srv.hits, 1)
		srv.lastReq = r.Clone(r.Context())
		if !strings.HasSuffix(r.URL.Path, "/events") {
			t.Fatalf("unexpected path %s", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"items":[
			{"id":"e1111111-1111-1111-1111-111111111111","email_id":"11111111-1111-1111-1111-111111111111","kind":"sent","payload":{},"occurred_at":"2026-07-01T12:00:00Z"},
			{"id":"e2222222-2222-2222-2222-222222222222","email_id":"11111111-1111-1111-1111-111111111111","kind":"delivered","payload":{"smtp_response":"250 OK"},"occurred_at":"2026-07-01T12:00:03Z"}
		]}`))
	}))
	defer srv.Close()

	out, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "events", "11111111-1111-1111-1111-111111111111",
	)
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"sent", "delivered", "2026-07-01T12:00:03Z"} {
		if !strings.Contains(out, want) {
			t.Errorf("output missing %q:\n%s", want, out)
		}
	}
	if got := atomic.LoadInt32(&srv.hits); got != 1 {
		t.Fatalf("expected 1 request, got %d", got)
	}
	if !strings.HasSuffix(srv.lastReq.URL.Path, "/emails/11111111-1111-1111-1111-111111111111/events") {
		t.Fatalf("unexpected route: %s", srv.lastReq.URL.Path)
	}
}

func TestEmailEventsRequiresID(t *testing.T) {
	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": "http://unused.invalid"},
		"email", "events",
	)
	if err == nil {
		t.Fatal("expected error when <email-id> is missing")
	}
}

func TestEmailEventsAllWalksPages(t *testing.T) {
	srv := &fakeServer{}
	srv.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n := atomic.AddInt32(&srv.hits, 1)
		w.Header().Set("Content-Type", "application/json")
		if n == 1 {
			if got := r.URL.Query().Get("cursor"); got != "" {
				t.Fatalf("first page must not carry a cursor, got %q", got)
			}
			_, _ = w.Write([]byte(`{"items":[
				{"id":"e1111111-1111-1111-1111-111111111111","email_id":"11111111-1111-1111-1111-111111111111","kind":"sent","payload":{},"occurred_at":"2026-07-01T12:00:00Z"}
			],"next_cursor":"cur-2"}`))
			return
		}
		if got := r.URL.Query().Get("cursor"); got != "cur-2" {
			t.Fatalf("second page must resume from next_cursor, got %q", got)
		}
		_, _ = w.Write([]byte(`{"items":[
			{"id":"e2222222-2222-2222-2222-222222222222","email_id":"11111111-1111-1111-1111-111111111111","kind":"delivered","payload":{},"occurred_at":"2026-07-01T12:00:03Z"}
		],"next_cursor":null}`))
	}))
	defer srv.Close()

	out, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "events", "--all", "11111111-1111-1111-1111-111111111111",
	)
	if err != nil {
		t.Fatal(err)
	}
	if got := atomic.LoadInt32(&srv.hits); got != 2 {
		t.Fatalf("expected 2 requests, got %d", got)
	}
	for _, want := range []string{"sent", "delivered"} {
		if !strings.Contains(out, want) {
			t.Errorf("output missing %q:\n%s", want, out)
		}
	}
	if strings.Contains(out, "more: --cursor") {
		t.Errorf("--all output must not suggest manual pagination:\n%s", out)
	}
}

func TestEmailEventsSinglePageShowsCursorHint(t *testing.T) {
	srv := &fakeServer{}
	srv.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&srv.hits, 1)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"items":[
			{"id":"e1111111-1111-1111-1111-111111111111","email_id":"11111111-1111-1111-1111-111111111111","kind":"sent","payload":{},"occurred_at":"2026-07-01T12:00:00Z"}
		],"next_cursor":"cur-2"}`))
	}))
	defer srv.Close()

	out, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "events", "11111111-1111-1111-1111-111111111111",
	)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out, "more: --cursor cur-2") {
		t.Errorf("expected cursor hint in output:\n%s", out)
	}
	if got := atomic.LoadInt32(&srv.hits); got != 1 {
		t.Fatalf("expected 1 request, got %d", got)
	}
}
