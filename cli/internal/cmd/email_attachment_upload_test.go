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

// TestEmailAttachmentUpload_OversizeRejection guards against a real bug: the
// server's size-cap rejection returns a plain string `detail`
// ({"detail": "..."}), not FastAPI's list-shaped validation-error detail
// ({"detail": [...]}) that the generated WithResponse parser expects for
// every 422. Using that generated parser directly made the real "too large"
// message get replaced by a raw Go json.Unmarshal error. This must surface
// the actual server message instead.
func TestEmailAttachmentUpload_OversizeRejection(t *testing.T) {
	srv := newFakeServer(t, http.StatusUnprocessableEntity, map[string]string{
		"detail": "attachment(s) too large — host the file externally and include a link in the body instead",
	})

	dir := t.TempDir()
	filePath := filepath.Join(dir, "big.pdf")
	if err := os.WriteFile(filePath, []byte("pdf bytes"), 0o644); err != nil {
		t.Fatalf("write file: %v", err)
	}

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "attachment-upload", filePath,
	)
	if err == nil {
		t.Fatal("expected error on 422")
	}
	if strings.Contains(err.Error(), "cannot unmarshal") {
		t.Fatalf("leaked raw json unmarshal error instead of the server message: %v", err)
	}
	if !strings.Contains(err.Error(), "too large") {
		t.Fatalf("expected the real oversize message, got: %v", err)
	}
}
