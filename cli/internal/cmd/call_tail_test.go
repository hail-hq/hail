package cmd

import (
	"net/http"
	"strings"
	"testing"
)

func TestCallTail_FullUUID_DelegatesToTail(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, map[string]any{"items": []any{}, "next_cursor": nil})
	id := "11111111-1111-1111-1111-111111111111"

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"call", "tail", id, "--no-follow",
	)
	if err != nil {
		t.Fatalf("unexpected: %v", err)
	}
	q := srv.lastReq.URL.Query().Get("id")
	if !strings.HasPrefix(q, "call:") || !strings.HasSuffix(q, id) {
		t.Fatalf("expected call:<id>, got %q", q)
	}
}

func TestCallTail_MissingArg_ShowsHelp(t *testing.T) {
	_, stderr, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test"},
		"call", "tail",
	)
	if err == nil || !strings.Contains(stderr, "missing required: <call-id>") {
		t.Fatalf("expected help-on-missing, got err=%v stderr=%q", err, stderr)
	}
}
