package cmd

import (
	"context"
	"fmt"
	"text/tabwriter"
	"time"

	"github.com/spf13/cobra"

	"github.com/hail-hq/hail/cli/internal/client"
)

type emailStatsFlags struct {
	from   string
	to     string
	bucket string
}

// newEmailStatsCmd builds `hail email stats` — account-level deliverability
// totals and rates over a window (default: trailing 7 days).
func newEmailStatsCmd(opts *Options) *cobra.Command {
	f := &emailStatsFlags{}
	cmd := &cobra.Command{
		Use:   "stats",
		Short: "Account-level email deliverability stats",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runEmailStats(cmd.Context(), opts, f)
		},
	}
	cmd.Flags().StringVar(&f.from, "from", "", "Window start (RFC 3339; default: server picks now-7d)")
	cmd.Flags().StringVar(&f.to, "to", "", "Window end (RFC 3339; default: now)")
	cmd.Flags().StringVar(&f.bucket, "bucket", "", "Bucket size for the series: hour|day (default: day)")
	return cmd
}

func runEmailStats(ctx context.Context, opts *Options, f *emailStatsFlags) error {
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}

	params := &client.GetEmailStatsEmailsStatsGetParams{}
	if f.from != "" {
		t, err := time.Parse(time.RFC3339, f.from)
		if err != nil {
			return fmt.Errorf("--from: %w", err)
		}
		params.From = &t
	}
	if f.to != "" {
		t, err := time.Parse(time.RFC3339, f.to)
		if err != nil {
			return fmt.Errorf("--to: %w", err)
		}
		params.To = &t
	}
	if f.bucket != "" {
		b := client.GetEmailStatsEmailsStatsGetParamsBucket(f.bucket)
		params.Bucket = &b
	}

	resp, err := apiClient.GetEmailStatsEmailsStatsGetWithResponse(ctx, params)
	if err != nil {
		return fmt.Errorf("email API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != 200 || resp.JSON200 == nil {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}

	return printEmailStats(opts, resp.JSON200)
}

// intOrZero dereferences an optional count field — the server always sends
// these, but the generated client marks them pointer-optional because the
// spec has no `required` list for aggregate counts (defaults are 0).
func intOrZero(v *int) int {
	if v == nil {
		return 0
	}
	return *v
}

// pctOrDash renders a rate as "NN.N%", or "-" when the server omitted it
// (EmailStatsRates fields are all nil when the window had zero sends).
func pctOrDash(v *float32) string {
	if v == nil {
		return "-"
	}
	return fmt.Sprintf("%.1f%%", *v*100)
}

func printEmailStats(opts *Options, s *client.EmailStatsResponse) error {
	if opts.JSON {
		return printJSON(opts.Stdout, s)
	}

	fmt.Fprintf(opts.Stdout, "Window: %s .. %s (bucket: %s)\n\n",
		s.From.UTC().Format(time.RFC3339), s.To.UTC().Format(time.RFC3339), string(s.Bucket))

	t := s.Totals
	r := s.Rates
	w := tabwriter.NewWriter(opts.Stdout, 0, 4, 2, ' ', 0)
	fmt.Fprintf(w, "sent\t%d\n", intOrZero(t.Sent))
	fmt.Fprintf(w, "delivered\t%d\t%s\n", intOrZero(t.Delivered), pctOrDash(r.Delivery))
	fmt.Fprintf(w, "delivery delayed\t%d\n", intOrZero(t.DeliveryDelayed))
	fmt.Fprintf(w, "bounced (hard)\t%d (%d)\t%s\n", intOrZero(t.Bounced), intOrZero(t.BouncedHard), pctOrDash(r.Bounce))
	fmt.Fprintf(w, "complained\t%d\t%s\n", intOrZero(t.Complained), pctOrDash(r.Complaint))
	fmt.Fprintf(w, "rejected\t%d\n", intOrZero(t.Rejected))
	fmt.Fprintf(w, "opened (unique)\t%d (%d)\t%s\n", intOrZero(t.Opened), intOrZero(t.UniqueOpened), pctOrDash(r.Open))
	fmt.Fprintf(w, "clicked (unique)\t%d (%d)\t%s\n", intOrZero(t.Clicked), intOrZero(t.UniqueClicked), pctOrDash(r.Click))
	return w.Flush()
}
