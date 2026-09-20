package cmd

import (
	"encoding/json"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/google/uuid"
	openapi_types "github.com/oapi-codegen/runtime/types"

	"github.com/hail-hq/hail/cli/internal/client"
)

func sampleEmailDomainResponse(kind string) client.EmailDomainResponse {
	id := openapi_types.UUID(uuid.MustParse("11111111-1111-1111-1111-111111111111"))
	orgID := openapi_types.UUID(uuid.MustParse("22222222-2222-2222-2222-222222222222"))
	now := time.Date(2026, 5, 17, 12, 0, 0, 0, time.UTC)
	user := "alice"
	org := "acme"
	resp := client.EmailDomainResponse{
		Id:                 id,
		OrganizationId:     orgID,
		Kind:               client.EmailDomainResponseKind(kind),
		Domain:             "alice+acme@mail.hail.so",
		VerificationStatus: client.EmailDomainResponseVerificationStatusVerified,
		DnsRecords:         []client.DnsRecordSchema{},
		Provider:           "ses",
		CreatedAt:          now,
		UpdatedAt:          now,
	}
	if kind == "hail_mail" {
		resp.LocalPrefixUser = &user
		resp.LocalPrefixOrg = &org
	} else {
		resp.Domain = "acme.com"
		resp.VerificationStatus = client.EmailDomainResponseVerificationStatusPending
		typ := client.DnsRecordSchemaTypeCNAME
		resp.DnsRecords = []client.DnsRecordSchema{
			{Name: "sel1._domainkey.acme.com", Value: "sel1.dkim.amazonses.com", Type: &typ},
			{Name: "sel2._domainkey.acme.com", Value: "sel2.dkim.amazonses.com", Type: &typ},
			{Name: "sel3._domainkey.acme.com", Value: "sel3.dkim.amazonses.com", Type: &typ},
		}
	}
	return resp
}

func TestEmailDomain_RegisterHailMail(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleEmailDomainResponse("hail_mail"))

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "domain", "register",
		"--kind", "hail_mail",
		"--local-prefix-user", "alice",
		"--local-prefix-org", "acme",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if got := atomic.LoadInt32(&srv.hits); got != 1 {
		t.Fatalf("expected 1 request, got %d", got)
	}
	if srv.lastReq.URL.Path != "/v1/email-domains" {
		t.Fatalf("unexpected path: %s", srv.lastReq.URL.Path)
	}

	var body client.EmailDomainCreate
	if err := json.Unmarshal(srv.lastBody, &body); err != nil {
		t.Fatalf("body parse: %v; raw=%s", err, srv.lastBody)
	}
	if body.Kind != "hail_mail" {
		t.Fatalf("Kind = %v", body.Kind)
	}
	if body.LocalPrefixUser == nil || *body.LocalPrefixUser != "alice" {
		t.Fatalf("LocalPrefixUser = %v", body.LocalPrefixUser)
	}
	if body.LocalPrefixOrg == nil || *body.LocalPrefixOrg != "acme" {
		t.Fatalf("LocalPrefixOrg = %v", body.LocalPrefixOrg)
	}
	if !strings.Contains(stdout, "alice+acme@mail.hail.so") {
		t.Errorf("stdout missing address: %q", stdout)
	}
}

func TestEmailDomain_RegisterCustomReturnsDkim(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleEmailDomainResponse("custom"))

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "domain", "register",
		"--kind", "custom",
		"--domain", "acme.com",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	var body client.EmailDomainCreate
	if err := json.Unmarshal(srv.lastBody, &body); err != nil {
		t.Fatalf("body parse: %v; raw=%s", err, srv.lastBody)
	}
	if body.Domain == nil || *body.Domain != "acme.com" {
		t.Fatalf("Domain = %v", body.Domain)
	}
	if !strings.Contains(stdout, "sel1._domainkey.acme.com") {
		t.Errorf("stdout missing DKIM record: %q", stdout)
	}
}

func TestEmailDomain_RegisterHailMailMinimalArgs(t *testing.T) {
	// Caller relies entirely on server-side env defaults (HAIL_MAIL_FROM
	// or HAIL_MAIL_DEFAULT_*_PREFIX). The wire body must NOT carry domain
	// or empty prefix fields — those would cause server-side validation
	// errors. strPtr("") returns nil, so omitting flags should omit fields.
	srv := newFakeServer(t, http.StatusCreated, sampleEmailDomainResponse("hail_mail"))

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "domain", "register", "--kind", "hail_mail",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	var body map[string]any
	if err := json.Unmarshal(srv.lastBody, &body); err != nil {
		t.Fatalf("body parse: %v; raw=%s", err, srv.lastBody)
	}
	if body["kind"] != "hail_mail" {
		t.Fatalf("kind = %v", body["kind"])
	}
	// Optional fields must be omitted, not sent as empty strings.
	for _, key := range []string{"domain", "local_prefix_user", "local_prefix_org"} {
		if _, present := body[key]; present {
			t.Errorf("%s should be omitted from minimal-args body, got %v", key, body[key])
		}
	}
}

func TestEmailDomain_RegisterRejectsBadKind(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleEmailDomainResponse("hail_mail"))

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "domain", "register", "--kind", "carrier-pigeon",
	)
	if err == nil {
		t.Fatal("expected error on invalid kind")
	}
	if got := atomic.LoadInt32(&srv.hits); got != 0 {
		t.Fatalf("server should not have been hit, got %d", got)
	}
}

func TestEmailDomain_List(t *testing.T) {
	listResp := client.EmailDomainListResponse{
		Items: []client.EmailDomainResponse{sampleEmailDomainResponse("hail_mail")},
	}
	srv := newFakeServer(t, http.StatusOK, listResp)

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "domain", "list",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(stdout, "alice+acme@mail.hail.so") {
		t.Errorf("stdout missing entry: %q", stdout)
	}
	if !strings.Contains(stdout, "KIND") {
		t.Errorf("stdout missing header: %q", stdout)
	}
}

func sampleDnsCheckResponse(withProvider bool, dmarcPresent bool) client.EmailDomainDnsCheck {
	cnameType := client.ObservedDnsRecordTypeCNAME
	suggestedTxtType := client.DnsRecordSchemaTypeTXT
	check := client.EmailDomainDnsCheck{
		Zone:     strPtr("acme.com"),
		LookupOk: true,
		Records: []client.ObservedDnsRecord{
			{
				Type:     &cnameType,
				Name:     "sel1._domainkey.acme.com",
				Value:    "sel1.dkim.amazonses.com",
				Observed: true,
			},
			{
				Type:     &cnameType,
				Name:     "sel2._domainkey.acme.com",
				Value:    "sel2.dkim.amazonses.com",
				Observed: false,
			},
		},
		Dmarc: client.DmarcCheck{
			Present: dmarcPresent,
		},
	}
	if !dmarcPresent {
		check.Dmarc.Suggested = &client.DnsRecordSchema{
			Type:  &suggestedTxtType,
			Name:  "_dmarc.acme.com",
			Value: "v=DMARC1; p=none;",
		}
	}
	if withProvider {
		note := "Records tab is under DNS > Records."
		check.DnsProvider = &client.DnsProviderSchema{
			Id:     "cloudflare",
			Name:   "Cloudflare",
			DnsUrl: "https://dash.cloudflare.com",
			Note:   &note,
		}
	}
	return check
}

func TestEmailDomain_DnsCheck_Human(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, sampleDnsCheckResponse(true, false))

	id := "11111111-1111-1111-1111-111111111111"
	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "domain", "dns-check", id,
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if srv.lastReq.URL.Path != "/v1/email-domains/"+id+"/dns-check" {
		t.Fatalf("unexpected path: %s", srv.lastReq.URL.Path)
	}
	if srv.lastReq.Method != http.MethodGet {
		t.Fatalf("expected GET, got %s", srv.lastReq.Method)
	}
	if !strings.Contains(stdout, "DNS host: Cloudflare — https://dash.cloudflare.com") {
		t.Errorf("stdout missing dns host line: %q", stdout)
	}
	if !strings.Contains(stdout, "Records tab is under DNS > Records.") {
		t.Errorf("stdout missing note: %q", stdout)
	}
	if !strings.Contains(stdout, "TYPE") || !strings.Contains(stdout, "SEEN") {
		t.Errorf("stdout missing table header: %q", stdout)
	}
	seenFor := func(name string) string {
		for _, line := range strings.Split(stdout, "\n") {
			if strings.Contains(line, name) {
				if f := strings.Fields(line); len(f) == 4 {
					return f[3]
				}
			}
		}
		return ""
	}
	if got := seenFor("sel1._domainkey.acme.com"); got != "yes" {
		t.Errorf("sel1 SEEN column = %q, want yes; stdout=%q", got, stdout)
	}
	if got := seenFor("sel2._domainkey.acme.com"); got != "no" {
		t.Errorf("sel2 SEEN column = %q, want no; stdout=%q", got, stdout)
	}
	if !strings.Contains(stdout, `DMARC: not found — add TXT _dmarc.acme.com "v=DMARC1; p=none;"`) {
		t.Errorf("stdout missing dmarc line: %q", stdout)
	}
}

func TestEmailDomain_DnsCheck_NoProvider(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, sampleDnsCheckResponse(false, true))

	id := "11111111-1111-1111-1111-111111111111"
	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "domain", "dns-check", id,
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(stdout, "DNS host: not recognised") {
		t.Errorf("stdout missing not-recognised line: %q", stdout)
	}
	if !strings.Contains(stdout, "DMARC: found") {
		t.Errorf("stdout missing dmarc found line: %q", stdout)
	}
}

func TestEmailDomain_DnsCheck_JSON(t *testing.T) {
	srv := newFakeServer(t, http.StatusOK, sampleDnsCheckResponse(true, false))

	id := "11111111-1111-1111-1111-111111111111"
	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"--json", "email", "domain", "dns-check", id,
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var body client.EmailDomainDnsCheck
	if err := json.Unmarshal([]byte(stdout), &body); err != nil {
		t.Fatalf("stdout not valid JSON: %v; raw=%s", err, stdout)
	}
	if body.DnsProvider == nil || body.DnsProvider.Id != "cloudflare" {
		t.Fatalf("DnsProvider = %+v", body.DnsProvider)
	}
	if len(body.Records) != 2 {
		t.Fatalf("Records len = %d", len(body.Records))
	}
}

func TestEmailDomain_DnsCheck_HailMail(t *testing.T) {
	check := client.EmailDomainDnsCheck{
		Zone:     nil,
		LookupOk: true,
		Records:  []client.ObservedDnsRecord{},
		Dmarc:    client.DmarcCheck{Present: false, Suggested: nil},
	}
	srv := newFakeServer(t, http.StatusOK, check)

	id := "11111111-1111-1111-1111-111111111111"
	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "domain", "dns-check", id,
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if stdout != "DNS host: not recognised\n" {
		t.Fatalf("stdout = %q, want exactly the not-recognised line and nothing else", stdout)
	}
}

func TestEmailDomain_DnsCheck_LookupNotOk(t *testing.T) {
	check := sampleDnsCheckResponse(true, false)
	check.LookupOk = false
	srv := newFakeServer(t, http.StatusOK, check)

	id := "11111111-1111-1111-1111-111111111111"
	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "domain", "dns-check", id,
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	want := "DNS check could not be completed. Try again in a minute.\n"
	if stdout != want {
		t.Fatalf("stdout = %q, want exactly %q", stdout, want)
	}
}

func TestEmailDomain_DnsCheck_LookupNotOk_JSONPrintsBodyAsIs(t *testing.T) {
	check := sampleDnsCheckResponse(true, false)
	check.LookupOk = false
	srv := newFakeServer(t, http.StatusOK, check)

	id := "11111111-1111-1111-1111-111111111111"
	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"--json", "email", "domain", "dns-check", id,
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var body client.EmailDomainDnsCheck
	if err := json.Unmarshal([]byte(stdout), &body); err != nil {
		t.Fatalf("stdout not valid JSON: %v; raw=%s", err, stdout)
	}
	if body.LookupOk {
		t.Fatalf("LookupOk = true, want false (body printed as-is)")
	}
	if body.DnsProvider == nil || body.DnsProvider.Id != "cloudflare" {
		t.Fatalf("DnsProvider = %+v, want the raw body unchanged", body.DnsProvider)
	}
}

func TestEmailDomain_Verify(t *testing.T) {
	verified := sampleEmailDomainResponse("custom")
	verified.VerificationStatus = client.EmailDomainResponseVerificationStatusVerified
	srv := newFakeServer(t, http.StatusOK, verified)

	id := "11111111-1111-1111-1111-111111111111"
	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "domain", "verify", id,
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if srv.lastReq.URL.Path != "/v1/email-domains/"+id+"/verify" {
		t.Fatalf("unexpected path: %s", srv.lastReq.URL.Path)
	}
	if srv.lastReq.Method != http.MethodPost {
		t.Fatalf("expected POST, got %s", srv.lastReq.Method)
	}
}

func TestEmailDomain_Delete(t *testing.T) {
	srv := newFakeServer(t, http.StatusNoContent, nil)

	id := "11111111-1111-1111-1111-111111111111"
	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "domain", "delete", id,
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if srv.lastReq.Method != http.MethodDelete {
		t.Fatalf("expected DELETE, got %s", srv.lastReq.Method)
	}
	if !strings.Contains(stdout, "deleted") {
		t.Errorf("stdout missing confirmation: %q", stdout)
	}
}

func TestEmailDomain_DeleteInvalidUUID(t *testing.T) {
	srv := newFakeServer(t, http.StatusNoContent, nil)

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "domain", "delete", "not-a-uuid",
	)
	if err == nil {
		t.Fatal("expected error on invalid uuid")
	}
	if got := atomic.LoadInt32(&srv.hits); got != 0 {
		t.Fatalf("server should not have been hit, got %d", got)
	}
}
