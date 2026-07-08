package cmd

import (
	"fmt"
	"time"

	"github.com/spf13/cobra"
)

// consentFlags is the shared --recipient-consent / --consent-source /
// --consent-obtained-at / --message-type wiring used by `hail call`,
// `hail email send`, and `hail sms`. One home for the flag set and the
// CLI-side consent rules so a rule change cannot land in one command and
// silently miss the other two (exactly what b782a49 had to fix by hand
// across all three).
type consentFlags struct {
	recipientConsent  bool
	consentSource     string
	consentObtainedAt string
	messageType       string
}

// registerConsentFlags binds the four flags on cmd. noun names the message
// kind in help text ("call", "email", "text"). --recipient-consent is
// marked required so cobra enforces its presence before RunE.
func (f *consentFlags) registerConsentFlags(cmd *cobra.Command, noun string) {
	cmd.Flags().BoolVar(&f.recipientConsent, "recipient-consent", false,
		fmt.Sprintf("Confirm the recipient has consented to receive this %s — required by the API", noun))
	cmd.Flags().StringVar(&f.consentSource, "consent-source", "",
		"Where/how consent was obtained — required if --message-type=marketing")
	cmd.Flags().StringVar(&f.consentObtainedAt, "consent-obtained-at", "",
		"RFC 3339 timestamp consent was obtained at")
	cmd.Flags().StringVar(&f.messageType, "message-type", "",
		"\"marketing\" or \"informational\" (default: informational)")
	cmd.MarkFlagRequired("recipient-consent")
}

// validateConsent enforces the CLI-side consent rules (mirrors the API's
// enforce_consent) and parses --consent-obtained-at. Returns the parsed
// timestamp, or nil when the flag was unset.
func (f *consentFlags) validateConsent(cmd *cobra.Command) (*time.Time, error) {
	if err := requireTrueFlag(cmd, f.recipientConsent, "--recipient-consent"); err != nil {
		return nil, err
	}
	if f.messageType == "marketing" && f.consentSource == "" {
		return nil, requireInputs(cmd, "--consent-source (required when --message-type=marketing)")
	}
	if f.consentObtainedAt == "" {
		return nil, nil
	}
	t, err := time.Parse(time.RFC3339, f.consentObtainedAt)
	if err != nil {
		return nil, fmt.Errorf("--consent-obtained-at: invalid RFC 3339 timestamp: %w", err)
	}
	return &t, nil
}
