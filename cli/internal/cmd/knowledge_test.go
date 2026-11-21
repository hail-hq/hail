package cmd

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestKnowledgeAdd_UploadsThenAdds(t *testing.T) {
	var putBody []byte
	var putAuth string
	var addBody map[string]any
	var linkBody map[string]any
	var srv *httptest.Server
	srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/v1/uploads":
			_ = json.Unmarshal(body, &linkBody)
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(map[string]any{
				"id": "55555555-5555-5555-5555-555555555555", "purpose": "knowledge_document",
				"upload_url": srv.URL + "/v1/uploads/55555555-5555-5555-5555-555555555555?token=t",
				"method":     "PUT", "content_type": "application/pdf", "max_bytes": 1000,
				"expires_at": time.Now().Add(time.Minute),
			})
		case r.Method == http.MethodPut:
			putBody, putAuth = body, r.Header.Get("Authorization")
			_ = json.NewEncoder(w).Encode(map[string]any{"id": "55555555-5555-5555-5555-555555555555"})
		case r.Method == http.MethodPost && r.URL.Path == "/v1/knowledge/documents":
			_ = json.Unmarshal(body, &addBody)
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(map[string]any{
				"id": "66666666-6666-6666-6666-666666666666", "name": "Manual",
				"content_type": "application/pdf", "size_bytes": 10, "status": "pending",
				"error": nil, "piece_count": 0,
				"created_at": time.Now(), "updated_at": time.Now(),
			})
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	t.Cleanup(srv.Close)
	file := filepath.Join(t.TempDir(), "manual.pdf")
	if err := os.WriteFile(file, []byte("%PDF-1.4 x"), 0o600); err != nil {
		t.Fatal(err)
	}
	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"knowledge", "add", file, "--name", "Manual",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if linkBody["purpose"] != "knowledge_document" || linkBody["content_type"] != "application/pdf" {
		t.Errorf("link body: %v", linkBody)
	}
	if string(putBody) != "%PDF-1.4 x" || putAuth != "" {
		t.Errorf("put body %q auth %q", putBody, putAuth)
	}
	if addBody["upload_id"] != "55555555-5555-5555-5555-555555555555" || addBody["name"] != "Manual" {
		t.Errorf("add body: %v", addBody)
	}
	if !strings.Contains(stdout, "pending") || !strings.Contains(stdout, "hail knowledge get") {
		t.Errorf("stdout: %q", stdout)
	}
}

func TestKnowledgeAdd_UnknownExtensionNeedsAType(t *testing.T) {
	file := filepath.Join(t.TempDir(), "notes.rtf")
	_ = os.WriteFile(file, []byte("x"), 0o600)
	_, _, err := runRoot(t, map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": "http://127.0.0.1:1"},
		"knowledge", "add", file)
	if err == nil || !strings.Contains(err.Error(), "--content-type") {
		t.Fatalf("err: %v", err)
	}
}

func TestKnowledgeSearch_PrintsPieces(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, map[string]any{"results": []map[string]any{{
		"document_id": "66666666-6666-6666-6666-666666666666", "document_name": "Manual",
		"text": "E-204 means grid voltage out of range.", "page": 4, "row": nil, "score": 0.03,
	}}})
	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"knowledge", "search", "errore E 204", "--limit", "3",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if srv.lastReq.URL.Path != "/v1/knowledge/search" || !strings.Contains(string(srv.lastBody), `"limit":3`) {
		t.Errorf("request: %s %s", srv.lastReq.URL.Path, srv.lastBody)
	}
	if !strings.Contains(stdout, "Manual, page 4") {
		t.Errorf("stdout: %q", stdout)
	}
}

func TestAgentsUpdate_Knowledge(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, sampleAgent())
	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"agents", "update", "33333333-3333-3333-3333-333333333333", "--knowledge",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var body map[string]any
	_ = json.Unmarshal(srv.lastBody, &body)
	if body["knowledge"] != true {
		t.Errorf("body: %s", srv.lastBody)
	}
}
