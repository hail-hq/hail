package cmd

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestMcpEndpoint_TextForm_DerivesFromAPIURL(t *testing.T) {
	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_URL": "https://api.hail.so"},
		"mcp", "endpoint",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	// docs/setup/mcp.md is explicit: the Streamable HTTP transport serves at
	// the root path, no `/mcp` suffix. Assert both presence of the canonical
	// host and absence of any path append.
	if !strings.Contains(stdout, "https://mcp.hail.so") || strings.Contains(stdout, "mcp.hail.so/") {
		t.Fatalf("expected bare https://mcp.hail.so with no suffix, got %q", stdout)
	}
}

func TestMcpEndpoint_JSON(t *testing.T) {
	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_URL": "https://api.hail.so"},
		"mcp", "endpoint", "--json",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var v struct {
		URL       string `json:"url"`
		Transport string `json:"transport"`
	}
	if err := json.Unmarshal([]byte(stdout), &v); err != nil {
		t.Fatalf("expected JSON, got %q (err: %v)", stdout, err)
	}
	if v.URL != "https://mcp.hail.so" {
		t.Fatalf("url mismatch: %q (canonical per docs/setup/mcp.md is root path, no /mcp)", v.URL)
	}
	if v.Transport != "streamable-http" {
		t.Fatalf("transport mismatch: %q", v.Transport)
	}
}

func TestMcpEndpoint_NoCredsTolerated(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	_, _, err := runRoot(t, map[string]string{}, "mcp", "endpoint")
	if err != nil {
		t.Fatalf("expected no-auth tolerance, got %v", err)
	}
}

func TestMcpEndpoint_SelfHostFallback(t *testing.T) {
	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_URL": "http://localhost:8080", "HAIL_MCP_URL": "http://localhost:8081"},
		"mcp", "endpoint",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(stdout, "http://localhost:8081") {
		t.Fatalf("expected self-host override: %q", stdout)
	}
}
