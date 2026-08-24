package cmd

import (
	"bytes"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/hail-hq/hail/cli/internal/client"
)

func sampleManualContact() client.ContactEntry {
	phone := "+15551234567"
	email := "jane@example.com"
	return client.ContactEntry{
		Id:        "11111111-1111-1111-1111-111111111111",
		Kind:      client.Manual,
		Name:      "Jane Doe",
		PhoneE164: &phone,
		Email:     &email,
	}
}

func sampleMemberContact() client.ContactEntry {
	phone := "+15557654321"
	role := "owner"
	return client.ContactEntry{
		Id:        "member:22222222-2222-2222-2222-222222222222",
		Kind:      client.Member,
		Name:      "Alice Admin",
		PhoneE164: &phone,
		Role:      &role,
	}
}

// --------------------------------------------------------------------------- //
// list
// --------------------------------------------------------------------------- //

func TestContactsList_HappyPath(t *testing.T) {
	listResp := client.ContactListResponse{
		Items: []client.ContactEntry{sampleManualContact(), sampleMemberContact()},
	}
	srv := newFakeServer(t, http.StatusOK, listResp)

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"contacts", "list",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if srv.lastReq.Method != http.MethodGet || srv.lastReq.URL.Path != "/v1/contacts" {
		t.Fatalf("unexpected route: %s %s", srv.lastReq.Method, srv.lastReq.URL.Path)
	}
	if !strings.Contains(stdout, "Jane Doe") || !strings.Contains(stdout, "Alice Admin") {
		t.Errorf("stdout missing entries: %q", stdout)
	}
	if !strings.Contains(stdout, "NAME") || !strings.Contains(stdout, "KIND") {
		t.Errorf("stdout missing header: %q", stdout)
	}
}

func TestContactsList_QAndLimitFlags(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, client.ContactListResponse{})

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"contacts", "list", "--q", "jane", "--limit", "10",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	q := srv.lastReq.URL.Query()
	if q.Get("q") != "jane" {
		t.Errorf("q = %q, want jane", q.Get("q"))
	}
	if q.Get("limit") != "10" {
		t.Errorf("limit = %q, want 10", q.Get("limit"))
	}
}

func TestContactsList_Empty(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, client.ContactListResponse{})

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"contacts", "list",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(stdout, "no contacts") {
		t.Errorf("stdout = %q", stdout)
	}
}

// --------------------------------------------------------------------------- //
// create
// --------------------------------------------------------------------------- //

func TestContactsCreate_HappyPath(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleManualContact())

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"contacts", "create", "Jane Doe", "--phone", "+15551234567", "--email", "jane@example.com",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if srv.lastReq.Method != http.MethodPost || srv.lastReq.URL.Path != "/v1/contacts" {
		t.Fatalf("unexpected route: %s %s", srv.lastReq.Method, srv.lastReq.URL.Path)
	}
	if h := srv.lastReq.Header.Get("Idempotency-Key"); h == "" {
		t.Error("Idempotency-Key header missing")
	}

	var body client.ContactCreate
	if err := json.Unmarshal(srv.lastBody, &body); err != nil {
		t.Fatalf("body parse: %v; raw=%s", err, srv.lastBody)
	}
	if body.Name != "Jane Doe" {
		t.Errorf("Name = %q", body.Name)
	}
	if body.PhoneE164 == nil || *body.PhoneE164 != "+15551234567" {
		t.Errorf("PhoneE164 = %v", body.PhoneE164)
	}
	if body.Email == nil || *body.Email != "jane@example.com" {
		t.Errorf("Email = %v", body.Email)
	}
	if !strings.Contains(stdout, "Jane Doe") {
		t.Errorf("stdout missing name: %q", stdout)
	}
}

func TestContactsCreate_PhoneOnly(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleManualContact())

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"contacts", "create", "Jane Doe", "--phone", "+15551234567",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	var body map[string]any
	if err := json.Unmarshal(srv.lastBody, &body); err != nil {
		t.Fatalf("body parse: %v", err)
	}
	if _, present := body["email"]; present {
		t.Errorf("email should be omitted, got %v", body["email"])
	}
}

func TestContactsCreate_EmailOnly(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleManualContact())

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"contacts", "create", "Jane Doe", "--email", "jane@example.com",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	var body map[string]any
	if err := json.Unmarshal(srv.lastBody, &body); err != nil {
		t.Fatalf("body parse: %v", err)
	}
	if _, present := body["phone_e164"]; present {
		t.Errorf("phone_e164 should be omitted, got %v", body["phone_e164"])
	}
}

// TestContactsCreate_RequiresPhoneOrEmail pins the client-side mirror of the
// API's 422 for a contact with neither: the CLI fails locally, before any
// HTTP request, rather than round-tripping to hit the server's validation.
func TestContactsCreate_RequiresPhoneOrEmail(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleManualContact())

	_, stderr, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"contacts", "create", "Jane Doe",
	)
	if !errors.Is(err, errInvalidInputs) {
		t.Fatalf("want errInvalidInputs, got %v", err)
	}
	if !strings.Contains(stderr, "--phone or --email") {
		t.Errorf("stderr missing reason: %q", stderr)
	}
	if got := atomic.LoadInt32(&srv.hits); got != 0 {
		t.Fatalf("server should not have been called, got %d hits", got)
	}
}

func TestContactsCreate_MissingName_PrintsHelpAndFails(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleManualContact())

	_, stderr, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"contacts", "create", "--phone", "+15551234567",
	)
	if !errors.Is(err, errInvalidInputs) {
		t.Fatalf("want errInvalidInputs, got %v", err)
	}
	if !strings.Contains(stderr, "missing required: <name>") {
		t.Errorf("stderr = %q", stderr)
	}
	if got := atomic.LoadInt32(&srv.hits); got != 0 {
		t.Fatalf("server should not have been called, got %d hits", got)
	}
}

func TestContactsCreate_APIError(t *testing.T) {
	errBody := client.HTTPValidationError{
		Detail: &[]client.ValidationError{{
			Loc:  []client.ValidationError_Loc_Item{},
			Msg:  "at least one of phone_e164 or email is required",
			Type: "value_error",
		}},
	}
	srv := newFakeServer(t, http.StatusUnprocessableEntity, errBody)

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"contacts", "create", "Jane Doe", "--phone", "+15551234567",
	)
	if err == nil {
		t.Fatal("expected error from server")
	}
	if !strings.Contains(err.Error(), "at least one of phone_e164 or email is required") {
		t.Errorf("error = %v; expected detail message", err)
	}
}

// --------------------------------------------------------------------------- //
// update
// --------------------------------------------------------------------------- //

func TestContactsUpdate_HappyPath(t *testing.T) {
	updated := sampleManualContact()
	newPhone := "+15559876543"
	updated.PhoneE164 = &newPhone
	srv := newFakeServer(t, http.StatusOK, updated)

	id := "11111111-1111-1111-1111-111111111111"
	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"contacts", "update", id, "--phone", "+15559876543",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if srv.lastReq.Method != http.MethodPatch || srv.lastReq.URL.Path != "/v1/contacts/"+id {
		t.Fatalf("unexpected route: %s %s", srv.lastReq.Method, srv.lastReq.URL.Path)
	}

	var body client.ContactPatch
	if err := json.Unmarshal(srv.lastBody, &body); err != nil {
		t.Fatalf("body parse: %v; raw=%s", err, srv.lastBody)
	}
	if body.PhoneE164 == nil || *body.PhoneE164 != "+15559876543" {
		t.Errorf("PhoneE164 = %v", body.PhoneE164)
	}
	if body.Name != nil {
		t.Errorf("Name should be nil, got %v", *body.Name)
	}
	if !strings.Contains(stdout, "+15559876543") {
		t.Errorf("stdout missing new phone: %q", stdout)
	}
}

func TestContactsUpdate_NameAndEmail(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, sampleManualContact())

	id := "11111111-1111-1111-1111-111111111111"
	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"contacts", "update", id, "--name", "Jane Q. Doe", "--email", "jane.q@example.com",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	var body client.ContactPatch
	if err := json.Unmarshal(srv.lastBody, &body); err != nil {
		t.Fatalf("body parse: %v", err)
	}
	if body.Name == nil || *body.Name != "Jane Q. Doe" {
		t.Errorf("Name = %v", body.Name)
	}
	if body.Email == nil || *body.Email != "jane.q@example.com" {
		t.Errorf("Email = %v", body.Email)
	}
	if body.PhoneE164 != nil {
		t.Errorf("PhoneE164 should be nil, got %v", *body.PhoneE164)
	}
}

func TestContactsUpdate_MissingID_PrintsHelp(t *testing.T) {
	_, stderr, err := runRoot(t, map[string]string{"HAIL_API_KEY": "sk_test"}, "contacts", "update")
	if !errors.Is(err, errInvalidInputs) {
		t.Fatalf("want errInvalidInputs, got %v", err)
	}
	if !strings.Contains(stderr, "missing required: <id>") {
		t.Fatalf("stderr = %q", stderr)
	}
}

// TestContactsUpdate_RequiresAField pins the local guard against a no-op
// update: zero flags would send an empty PATCH, get the unchanged row back
// with 200, and report success without changing anything.
func TestContactsUpdate_RequiresAField(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, sampleManualContact())

	_, stderr, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"contacts", "update", "11111111-1111-1111-1111-111111111111",
	)
	if !errors.Is(err, errInvalidInputs) {
		t.Fatalf("want errInvalidInputs, got %v", err)
	}
	if !strings.Contains(stderr, "--name, --phone, or --email") {
		t.Errorf("stderr missing reason: %q", stderr)
	}
	if got := atomic.LoadInt32(&srv.hits); got != 0 {
		t.Fatalf("server should not have been called, got %d hits", got)
	}
}

// --------------------------------------------------------------------------- //
// delete
// --------------------------------------------------------------------------- //

func TestContactsDelete_HappyPath(t *testing.T) {
	srv := newFakeServer(t, http.StatusNoContent, nil)

	id := "11111111-1111-1111-1111-111111111111"
	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"contacts", "delete", id,
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if srv.lastReq.Method != http.MethodDelete || srv.lastReq.URL.Path != "/v1/contacts/"+id {
		t.Fatalf("unexpected route: %s %s", srv.lastReq.Method, srv.lastReq.URL.Path)
	}
	if !strings.Contains(stdout, "deleted") {
		t.Errorf("stdout missing confirmation: %q", stdout)
	}
}

func TestContactsDelete_RmAlias(t *testing.T) {
	root := NewRootCmd(&bytes.Buffer{}, &bytes.Buffer{}, func(string) string { return "" })
	contacts, _, err := root.Find([]string{"contacts"})
	if err != nil {
		t.Fatalf("hail contacts: %v", err)
	}
	found := false
	for _, c := range contacts.Commands() {
		if c.Name() == "delete" {
			for _, a := range c.Aliases {
				if a == "rm" {
					found = true
				}
			}
		}
	}
	if !found {
		t.Error(`"delete" subcommand missing "rm" alias`)
	}
}

func TestContactsDelete_MemberIDPassesThrough(t *testing.T) {
	// Member ids ("member:<user_id>") are opaque strings to the CLI; it
	// forwards whatever the caller supplies and lets the API decide
	// whether the operation is valid for that kind of row.
	srv := newFakeServer(t, http.StatusUnprocessableEntity, client.HTTPValidationError{
		Detail: &[]client.ValidationError{{Msg: "cannot delete a member contact row", Type: "value_error"}},
	})

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"contacts", "delete", "member:22222222-2222-2222-2222-222222222222",
	)
	if err == nil {
		t.Fatal("expected error from server")
	}
	if srv.lastReq.URL.Path != "/v1/contacts/member:22222222-2222-2222-2222-222222222222" {
		t.Errorf("unexpected route: %s", srv.lastReq.URL.Path)
	}
}

// --------------------------------------------------------------------------- //
// set-phone / clear-phone
// --------------------------------------------------------------------------- //

func TestContactsSetPhone_HappyPath(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, map[string]string{"phone_e164": "+15551234567"})

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"contacts", "set-phone", "me", "--phone", "+15551234567",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if srv.lastReq.Method != http.MethodPut || srv.lastReq.URL.Path != "/v1/members/me/phone" {
		t.Fatalf("unexpected route: %s %s", srv.lastReq.Method, srv.lastReq.URL.Path)
	}

	var body client.MemberPhonePut
	if err := json.Unmarshal(srv.lastBody, &body); err != nil {
		t.Fatalf("body parse: %v; raw=%s", err, srv.lastBody)
	}
	if body.PhoneE164 != "+15551234567" {
		t.Errorf("PhoneE164 = %q", body.PhoneE164)
	}
	if !strings.Contains(stdout, "+15551234567") {
		t.Errorf("stdout missing phone: %q", stdout)
	}
}

func TestContactsSetPhone_ExplicitUserID(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, map[string]string{"phone_e164": "+15557654321"})

	userID := "22222222-2222-2222-2222-222222222222"
	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"contacts", "set-phone", userID, "--phone", "+15557654321",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if srv.lastReq.URL.Path != "/v1/members/"+userID+"/phone" {
		t.Fatalf("unexpected route: %s", srv.lastReq.URL.Path)
	}
}

func TestContactsSetPhone_RequiresPhoneFlag(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, map[string]string{})

	_, stderr, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"contacts", "set-phone", "me",
	)
	if !errors.Is(err, errInvalidInputs) {
		t.Fatalf("want errInvalidInputs, got %v", err)
	}
	if !strings.Contains(stderr, `required flag(s) "phone" not set`) {
		t.Errorf("stderr = %q", stderr)
	}
	if got := atomic.LoadInt32(&srv.hits); got != 0 {
		t.Fatalf("server should not have been called, got %d hits", got)
	}
}

func TestContactsClearPhone_HappyPath(t *testing.T) {
	srv := newFakeServer(t, http.StatusNoContent, nil)

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"contacts", "clear-phone", "me",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if srv.lastReq.Method != http.MethodDelete || srv.lastReq.URL.Path != "/v1/members/me/phone" {
		t.Fatalf("unexpected route: %s %s", srv.lastReq.Method, srv.lastReq.URL.Path)
	}
	if !strings.Contains(stdout, "cleared") {
		t.Errorf("stdout missing confirmation: %q", stdout)
	}
}

func TestContactsClearPhone_ExplicitUserID(t *testing.T) {
	srv := newFakeServer(t, http.StatusNoContent, nil)

	userID := "22222222-2222-2222-2222-222222222222"
	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"contacts", "clear-phone", userID,
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if srv.lastReq.URL.Path != "/v1/members/"+userID+"/phone" {
		t.Fatalf("unexpected route: %s", srv.lastReq.URL.Path)
	}
}

// --------------------------------------------------------------------------- //
// JSON output
// --------------------------------------------------------------------------- //

func TestContactsCreate_JSONOutput(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleManualContact())

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"--json", "contacts", "create", "Jane Doe", "--phone", "+15551234567",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var got client.ContactEntry
	if err := json.Unmarshal([]byte(stdout), &got); err != nil {
		t.Fatalf("stdout is not valid JSON: %v\n%s", err, stdout)
	}
	if got.Name != "Jane Doe" {
		t.Errorf("Name = %q", got.Name)
	}
}
