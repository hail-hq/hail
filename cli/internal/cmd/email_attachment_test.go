package cmd

import (
	"bytes"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
)

func newAttachServer(t *testing.T, attID, payload string) *fakeServer {
	t.Helper()
	fs := &fakeServer{}
	fs.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&fs.hits, 1)
		fs.lastReq = r.Clone(r.Context())

		switch {
		case strings.Contains(r.URL.Path, "/attachments/"):
			w.Header().Set("Content-Type", "application/pdf")
			w.WriteHeader(http.StatusOK)
			_, _ = io.WriteString(w, payload)
		case strings.HasSuffix(r.URL.Path, "/emails/44444444-4444-4444-4444-444444444444"):
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_, _ = io.WriteString(w, `{
				"id":"44444444-4444-4444-4444-444444444444",
				"organization_id":"55555555-5555-5555-5555-555555555555",
				"from_address":"a@b","to_addresses":["c@d"],
				"subject":"x","status":"received","direction":"inbound",
				"requested_at":"2026-06-28T00:00:00Z",
				"attachments":[{"id":"`+attID+`","filename":"f.pdf","content_type":"application/pdf","size_bytes":4}]
			}`)
		case r.URL.Path == "/v1/emails":
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_, _ = io.WriteString(w, `{"items":[{"id":"44444444-4444-4444-4444-444444444444"}],"next_cursor":null}`)
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	t.Cleanup(fs.Close)
	return fs
}

func TestEmailAttachment_OutputToFile(t *testing.T) {
	attID := "66666666-6666-6666-6666-666666666666"
	srv := newAttachServer(t, attID, "PDF1")
	dir := t.TempDir()
	target := filepath.Join(dir, "out.bin")

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "attachment",
		"44444444-4444-4444-4444-444444444444",
		attID,
		"--output", target,
	)
	if err != nil {
		t.Fatalf("unexpected: %v", err)
	}
	got, err := os.ReadFile(target)
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	if !bytes.Equal(got, []byte("PDF1")) {
		t.Fatalf("body mismatch: %q", got)
	}
}

func TestEmailAttachment_PrefixMatchedWithinParent(t *testing.T) {
	attID := "77777777-7777-7777-7777-777777777777"
	srv := newAttachServer(t, attID, "PAYLOAD")
	dir := t.TempDir()
	target := filepath.Join(dir, "out.bin")

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "attachment",
		"44444444", // email prefix
		"7777",     // attachment prefix
		"--output", target,
	)
	if err != nil {
		t.Fatalf("unexpected: %v", err)
	}
	got, _ := os.ReadFile(target)
	if !bytes.Equal(got, []byte("PAYLOAD")) {
		t.Fatalf("body mismatch: %q", got)
	}
}
