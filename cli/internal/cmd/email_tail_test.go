package cmd

import (
	"net/http"
	"strings"
	"testing"
)

func TestEmailTail_FullUUID_DelegatesToTail(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, map[string]any{"items": []any{}, "next_cursor": nil})
	id := "22222222-2222-2222-2222-222222222222"

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "tail", id, "--no-follow",
	)
	if err != nil {
		t.Fatalf("unexpected: %v", err)
	}
	q := srv.lastReq.URL.Query().Get("id")
	if !strings.HasPrefix(q, "email:") || !strings.HasSuffix(q, id) {
		t.Fatalf("expected email:<id>, got %q", q)
	}
}

func TestEmailTail_MissingArg_ShowsHelp(t *testing.T) {
	_, stderr, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test"},
		"email", "tail",
	)
	if err == nil || !strings.Contains(stderr, "missing required: <email-id>") {
		t.Fatalf("expected help-on-missing, got err=%v stderr=%q", err, stderr)
	}
}
