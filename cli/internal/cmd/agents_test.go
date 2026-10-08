package cmd

import (
	"encoding/json"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	openapi_types "github.com/oapi-codegen/runtime/types"

	"github.com/hail-hq/hail/cli/internal/client"
)

func sampleAgent() client.AgentResponse {
	first := "How can I help?"
	return client.AgentResponse{
		Id:             openapi_types.UUID(uuid.MustParse("33333333-3333-3333-3333-333333333333")),
		OrganizationId: openapi_types.UUID(uuid.MustParse("44444444-4444-4444-4444-444444444444")),
		Name:           "Front desk",
		SystemPrompt:   "Book appointments.",
		FirstMessage:   &first,
		AiDisclosure:   true,
		SmsEnabled:     true,
		Status:         client.AgentResponseStatus("live"),
		VoiceConfig:    map[string]interface{}{},
		CreatedAt:      time.Now(),
		UpdatedAt:      time.Now(),
	}
}

func TestAgentsList_HappyPath(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, client.AgentListResponse{Items: []client.AgentResponse{sampleAgent()}})
	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"agents", "list",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if srv.lastReq.Method != http.MethodGet || srv.lastReq.URL.Path != "/v1/agents" {
		t.Fatalf("unexpected route: %s %s", srv.lastReq.Method, srv.lastReq.URL.Path)
	}
	if !strings.Contains(stdout, "Front desk") || !strings.Contains(stdout, "workspace default") {
		t.Errorf("stdout: %q", stdout)
	}
}

func TestAgentsCreate_SendsOnlyGivenFields(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleAgent())
	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"agents", "create", "Front desk", "--prompt", "Book appointments.",
		"--first-message", "How can I help?", "--max-minutes", "10", "--language", "fr",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var body map[string]any
	if err := json.Unmarshal(srv.lastBody, &body); err != nil {
		t.Fatalf("body: %v", err)
	}
	if body["name"] != "Front desk" || body["system_prompt"] != "Book appointments." {
		t.Errorf("body: %v", body)
	}
	if body["max_duration_seconds"] != float64(600) {
		t.Errorf("max_duration_seconds: %v", body["max_duration_seconds"])
	}
	if _, present := body["ai_disclosure_line"]; present {
		t.Errorf("ai_disclosure_line must be left out when not given: %v", body)
	}
	if vc, _ := body["voice_config"].(map[string]any); vc["language"] != "fr" {
		t.Errorf("voice_config: %v", body["voice_config"])
	}
}

func TestAgentsUpdate_SendsExplicitNulls(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, sampleAgent())
	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"agents", "update", "33333333-3333-3333-3333-333333333333", "--first-message", "", "--no-ai-line",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if srv.lastReq.Method != http.MethodPatch {
		t.Fatalf("method: %s", srv.lastReq.Method)
	}
	var body map[string]any
	_ = json.Unmarshal(srv.lastBody, &body)
	if v, present := body["first_message"]; !present || v != nil {
		t.Errorf("first_message must be an explicit null: %v", body)
	}
	if body["ai_disclosure"] != false {
		t.Errorf("ai_disclosure: %v", body)
	}
	if _, present := body["system_prompt"]; present {
		t.Errorf("system_prompt must not be sent: %v", body)
	}
}

func TestNumbersRoute_NoneDetaches(t *testing.T) {
	n := client.PhoneNumberResponse{
		Id: openapi_types.UUID(uuid.MustParse("55555555-5555-5555-5555-555555555555")), E164: "+14155550100",
		CountryCode: "US", NumberType: "local", Capabilities: []string{"voice", "sms"},
		ProvisioningState: "active", IsDedicated: true,
	}
	srv := newFakeServer(t, http.StatusOK, n)
	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"numbers", "route", "55555555-5555-5555-5555-555555555555",
		"--calls", "33333333-3333-3333-3333-333333333333", "--texts", "none",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if srv.lastReq.Method != http.MethodPatch || srv.lastReq.URL.Path != "/v1/numbers/55555555-5555-5555-5555-555555555555" {
		t.Fatalf("route: %s %s", srv.lastReq.Method, srv.lastReq.URL.Path)
	}
	var body map[string]any
	_ = json.Unmarshal(srv.lastBody, &body)
	if body["voice_agent_id"] != "33333333-3333-3333-3333-333333333333" {
		t.Errorf("voice_agent_id: %v", body)
	}
	if v, present := body["sms_agent_id"]; !present || v != nil {
		t.Errorf("sms_agent_id must be an explicit null: %v", body)
	}
}

func TestAgentsUpdate_VoiceKeepsTheRestOfVoiceConfig(t *testing.T) {
	a := sampleAgent()
	a.VoiceConfig = map[string]interface{}{"language": "fr", "tts": "elevenlabs"}
	srv := newFakeServer(t, http.StatusOK, a)
	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"agents", "update", "33333333-3333-3333-3333-333333333333", "--voice", "v2",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if srv.lastReq.Method != http.MethodPatch {
		t.Fatalf("method: %s", srv.lastReq.Method)
	}
	var body map[string]any
	_ = json.Unmarshal(srv.lastBody, &body)
	vc, _ := body["voice_config"].(map[string]any)
	if vc["voice_id"] != "v2" || vc["language"] != "fr" || vc["tts"] != "elevenlabs" {
		t.Errorf("voice_config must keep what --voice did not touch: %v", body["voice_config"])
	}
}

func TestAgentsUpdate_EmptyToolsMeansNone(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, sampleAgent())
	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"agents", "update", "33333333-3333-3333-3333-333333333333", "--tools", "",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var body map[string]any
	_ = json.Unmarshal(srv.lastBody, &body)
	tools, present := body["tools"].([]any)
	if !present || len(tools) != 0 {
		t.Errorf("tools must be an empty list, not null: %s", srv.lastBody)
	}
}

func TestAgentsUpdate_AllToolsSendsNull(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, sampleAgent())
	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"agents", "update", "33333333-3333-3333-3333-333333333333", "--all-tools",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var body map[string]any
	_ = json.Unmarshal(srv.lastBody, &body)
	v, present := body["tools"]
	if !present || v != nil {
		t.Errorf("tools must be sent as null: %s", srv.lastBody)
	}
}

func TestAgentsUpdate_AllToolsConflictsWithTools(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, sampleAgent())
	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"agents", "update", "33333333-3333-3333-3333-333333333333", "--all-tools", "--tools", "end_call",
	)
	if err == nil {
		t.Fatal("--all-tools with --tools must fail")
	}
}

func TestAgentsUpdate_HandoverBuildsContacts(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, sampleAgent())
	a := "55555555-5555-5555-5555-555555555555"
	b := "66666666-6666-6666-6666-666666666666"
	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"agents", "update", "33333333-3333-3333-3333-333333333333",
		"--handover", a+"=billing, refunds", "--handover", b+"=anything else",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var body map[string]any
	_ = json.Unmarshal(srv.lastBody, &body)
	got, _ := body["handover_contacts"].([]any)
	if len(got) != 2 {
		t.Fatalf("want 2 contacts: %s", srv.lastBody)
	}
	first := got[0].(map[string]any)
	if first["contact_id"] != a || first["note"] != "billing, refunds" {
		t.Errorf("bad first contact: %s", srv.lastBody)
	}
}

func TestAgentsUpdate_NoHandoverSendsEmptyList(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, sampleAgent())
	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"agents", "update", "33333333-3333-3333-3333-333333333333", "--no-handover",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var body map[string]any
	_ = json.Unmarshal(srv.lastBody, &body)
	got, present := body["handover_contacts"].([]any)
	if !present || len(got) != 0 {
		t.Errorf("handover_contacts must be an empty list: %s", srv.lastBody)
	}
}

func TestAgentsCreate_HandoverRejectsBadValue(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, sampleAgent())
	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"agents", "create", "Desk", "--prompt", "hi", "--handover", "not-a-uuid=billing",
	)
	if err == nil {
		t.Fatal("bad --handover must fail")
	}
}

func TestAgentsCreate_HandoverSendsContacts(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleAgent())
	a := "55555555-5555-5555-5555-555555555555"
	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"agents", "create", "Desk", "--prompt", "hi", "--handover", a+"=billing",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var body map[string]any
	_ = json.Unmarshal(srv.lastBody, &body)
	got, _ := body["handover_contacts"].([]any)
	if len(got) != 1 {
		t.Fatalf("want 1 contact: %s", srv.lastBody)
	}
	c := got[0].(map[string]any)
	if c["contact_id"] != a || c["note"] != "billing" {
		t.Errorf("bad contact: %s", srv.lastBody)
	}
}

func TestAgentsCreate_HandoverRejectsLongNote(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, sampleAgent())
	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"agents", "create", "Desk", "--prompt", "hi",
		"--handover", "55555555-5555-5555-5555-555555555555="+strings.Repeat("x", 201),
	)
	if err == nil || !strings.Contains(err.Error(), "200") {
		t.Fatalf("want a 200-char error, got %v", err)
	}
}
