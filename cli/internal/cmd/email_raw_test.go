package cmd

import (
	"bytes"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
)

func newRawServer(t *testing.T, body []byte) *fakeServer {
	t.Helper()
	fs := &fakeServer{}
	fs.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&fs.hits, 1)
		fs.lastReq = r.Clone(r.Context())
		if strings.HasSuffix(r.URL.Path, "/raw") {
			w.Header().Set("Content-Type", "message/rfc822")
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write(body)
			return
		}
		if r.URL.Path == "/emails" {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_, _ = io.WriteString(w, `{"items":[{"id":"33333333-3333-3333-3333-333333333333"}],"next_cursor":null}`)
			return
		}
		w.WriteHeader(http.StatusNotFound)
	}))
	t.Cleanup(fs.Close)
	return fs
}

func TestEmailRaw_StdoutDefault(t *testing.T) {
	rfc := []byte("From: alice@example.com\r\nSubject: hi\r\n\r\nbody\r\n")
	srv := newRawServer(t, rfc)
	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "raw", "33333333-3333-3333-3333-333333333333",
	)
	if err != nil {
		t.Fatalf("unexpected: %v", err)
	}
	if !bytes.Equal([]byte(stdout), rfc) {
		t.Fatalf("body mismatch: got %q want %q", stdout, rfc)
	}
}

func TestEmailRaw_OutputToFile(t *testing.T) {
	rfc := []byte("From: bob@example.com\r\n\r\nfile body\r\n")
	srv := newRawServer(t, rfc)
	dir := t.TempDir()
	target := filepath.Join(dir, "out.eml")

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "raw", "33333333-3333-3333-3333-333333333333", "--output", target,
	)
	if err != nil {
		t.Fatalf("unexpected: %v", err)
	}
	got, err := os.ReadFile(target)
	if err != nil {
		t.Fatalf("read output: %v", err)
	}
	if !bytes.Equal(got, rfc) {
		t.Fatalf("file mismatch: got %q want %q", got, rfc)
	}
}

func TestEmailRaw_JSONRejected(t *testing.T) {
	srv := newRawServer(t, []byte(""))
	_, stderr, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "raw", "33333333-3333-3333-3333-333333333333", "--json",
	)
	if !errors.Is(err, errInvalidInputs) {
		t.Fatalf("want errInvalidInputs, got %v", err)
	}
	if !strings.Contains(stderr, "--json is not supported on raw") {
		t.Fatalf("expected reason: %q", stderr)
	}
}
