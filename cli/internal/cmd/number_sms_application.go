package cmd

import (
	"encoding/json"
	"fmt"
	"net/http"

	"github.com/google/uuid"
	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/hail-hq/hail/cli/internal/client"
)

func newNumberGetSmsApplicationCmd(opts *Options) *cobra.Command {
	return &cobra.Command{
		Use:   "get-sms-application <verification-id>",
		Short: "Read your saved SMS application for correction",
		Long: `Read the private saved answers for your organization's SMS registration as JSON.
When its step is needs_information, correct those answers and use amend-sms
with the same verification id. Do not cancel and recreate the paid brand.
An older application may have no saved answers; collect the actual current
answers in that case. This read may recover existing sender details; it never
creates a brand or files a campaign.`,
		Args: argsOrHelp(1, "<verification-id>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			id, err := uuid.Parse(args[0])
			if err != nil {
				return fmt.Errorf("verification id must be a full UUID")
			}
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			resp, err := apiClient.GetVerificationApplicationWithResponse(cmd.Context(), openapi_types.UUID(id), &client.GetVerificationApplicationParams{})
			if err != nil {
				return fmt.Errorf("verifications API: %w", err)
			}
			if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
				return apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			return printJSON(opts.Stdout, resp.JSON200)
		},
	}
}

func newNumberAmendSmsCmd(opts *Options) *cobra.Command {
	var fields, address []string
	cmd := &cobra.Command{
		Use:   "amend-sms <verification-id>",
		Short: "Correct consent evidence on the same SMS registration",
		Long: `Repair a needs_information SMS application with its existing verification id.
Read get-sms-application and current requirements first. Supply the complete
corrected fields with --field key=value, not just the changed fields. Include
the complete actual address with --address key=value, using the saved address
when it is still correct.
The API refuses identity changes and preserves the paid brand; do not cancel
or create a new registration to bypass a refusal. A later campaign filing can
incur its normal review fee. The result includes the current registration step.
Maximum 100 fields and 64 KiB of fields/address; no documents are accepted.`,
		Args: argsOrHelp(1, "<verification-id>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			id, err := uuid.Parse(args[0])
			if err != nil {
				return fmt.Errorf("verification id must be a full UUID")
			}
			answers, err := parseKeyValues(fields, "--field")
			if err != nil {
				return err
			}
			if len(answers) == 0 || len(answers) > 100 {
				return fmt.Errorf("supply 1 to 100 complete application fields with --field")
			}
			addr, err := parseKeyValues(address, "--address")
			if err != nil {
				return err
			}
			if len(addr) == 0 {
				return fmt.Errorf("supply the complete actual address with --address; reuse saved address values only if still correct")
			}
			detail, err := json.Marshal(map[string]any{"fields": answers, "address": addr})
			if err != nil {
				return fmt.Errorf("encode application: %w", err)
			}
			if len(detail) > 64*1024 {
				return fmt.Errorf("fields and address must fit within 64 KiB")
			}
			body := client.SmsApplicationAmendRequest{Fields: answers}
			if len(addr) != 0 {
				allowed := map[string]bool{"customer_name": true, "street": true, "city": true, "region": true, "postal_code": true, "country_code": true}
				for key := range addr {
					if !allowed[key] {
						return fmt.Errorf("unsupported address key; use customer_name, street, city, region, postal_code or country_code")
					}
				}
				raw, err := json.Marshal(addr)
				if err != nil {
					return fmt.Errorf("encode address: %w", err)
				}
				var parsed client.Address
				if err := json.Unmarshal(raw, &parsed); err != nil {
					return fmt.Errorf("invalid address")
				}
				body.Address = &parsed
			}
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			resp, err := apiClient.AmendSmsApplicationWithResponse(cmd.Context(), openapi_types.UUID(id), &client.AmendSmsApplicationParams{}, body)
			if err != nil {
				return fmt.Errorf("amendment may have been saved; check the same verification id before retrying: %w", err)
			}
			if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
				return apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			if opts.JSON {
				return printJSON(opts.Stdout, resp.JSON200)
			}
			fmt.Fprintf(opts.Stdout, "✓ SMS registration %s: %s", resp.JSON200.Id.String(), resp.JSON200.State)
			if resp.JSON200.Step != nil {
				fmt.Fprintf(opts.Stdout, " (%s)", *resp.JSON200.Step)
			}
			fmt.Fprintln(opts.Stdout)
			return nil
		},
	}
	cmd.Flags().StringArrayVar(&fields, "field", nil, "Complete corrected registration field as key=value (repeatable)")
	cmd.Flags().StringArrayVar(&address, "address", nil, "Required actual address part as key=value (repeatable)")
	return cmd
}
