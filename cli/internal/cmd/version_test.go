package cmd

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestVersion_TextMatchesLongFlag(t *testing.T) {
	stdout, _, err := runRoot(t, map[string]string{}, "version")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.HasPrefix(stdout, "hail ") {
		t.Fatalf("expected 'hail X.Y.Z ...' line: %q", stdout)
	}
	if !strings.Contains(stdout, "(commit ") {
		t.Fatalf("expected commit detail: %q", stdout)
	}
}

func TestVersion_JSONShape(t *testing.T) {
	stdout, _, err := runRoot(t, map[string]string{}, "version", "--json")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var v struct {
		Version string `json:"version"`
		Commit  string `json:"commit"`
		Built   string `json:"built"`
	}
	if err := json.Unmarshal([]byte(stdout), &v); err != nil {
		t.Fatalf("expected JSON, got %q (err: %v)", stdout, err)
	}
	if v.Version == "" || v.Commit == "" || v.Built == "" {
		t.Fatalf("expected all three fields populated: %+v", v)
	}
}
