package cmd

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
)

func TestEmailStatsRendersTotalsAndRates(t *testing.T) {
	srv := &fakeServer{}
	srv.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&srv.hits, 1)
		srv.lastReq = r.Clone(r.Context())
		if r.URL.Path != "/emails/stats" {
			t.Fatalf("unexpected path %s", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"from":"2026-06-24T00:00:00Z","to":"2026-07-01T00:00:00Z",
			"bucket":"day",
			"totals":{"sent":100,"delivered":97,"delivery_delayed":1,"bounced":3,
				"bounced_hard":2,"complained":0,"rejected":0,"opened":40,"clicked":12,
				"unique_opened":35,"unique_clicked":10},
			"rates":{"delivery":0.97,"bounce":0.02,"complaint":0.0,"open":0.35,"click":0.10},
			"series":[]}`))
	}))
	defer srv.Close()

	out, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "stats",
	)
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"sent", "100", "97.0%", "2.0%"} {
		if !strings.Contains(out, want) {
			t.Errorf("output missing %q:\n%s", want, out)
		}
	}
	if got := atomic.LoadInt32(&srv.hits); got != 1 {
		t.Fatalf("expected 1 request, got %d", got)
	}
}

func TestEmailStatsPassesFromToBucketFlags(t *testing.T) {
	srv := &fakeServer{}
	srv.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&srv.hits, 1)
		srv.lastReq = r.Clone(r.Context())
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"from":"2026-06-24T00:00:00Z","to":"2026-07-01T00:00:00Z",
			"bucket":"hour","totals":{},"rates":{},"series":[]}`))
	}))
	defer srv.Close()

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "stats",
		"--from", "2026-06-24T00:00:00Z",
		"--to", "2026-07-01T00:00:00Z",
		"--bucket", "hour",
	)
	if err != nil {
		t.Fatal(err)
	}
	q := srv.lastReq.URL.Query()
	if q.Get("from") == "" || q.Get("to") == "" || q.Get("bucket") != "hour" {
		t.Fatalf("unexpected query: %s", srv.lastReq.URL.RawQuery)
	}
}
