package cmd

import (
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestEmailAttachmentUpload_HappyPath(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, map[string]any{
		"id":           "11111111-1111-1111-1111-111111111111",
		"filename":     "invoice.pdf",
		"content_type": "application/octet-stream",
		"size_bytes":   9,
	})

	dir := t.TempDir()
	filePath := filepath.Join(dir, "invoice.pdf")
	if err := os.WriteFile(filePath, []byte("pdf bytes"), 0o644); err != nil {
		t.Fatalf("write file: %v", err)
	}

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "attachment-upload", filePath,
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(stdout, "11111111-1111-1111-1111-111111111111") {
		t.Errorf("stdout missing id: %q", stdout)
	}
	if srv.lastReq.URL.Path != "/email-attachments" {
		t.Fatalf("unexpected route: %s", srv.lastReq.URL.Path)
	}
}
