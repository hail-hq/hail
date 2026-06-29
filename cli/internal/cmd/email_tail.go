package cmd

import (
	"context"
	"fmt"

	"github.com/spf13/cobra"
)

func newEmailTailCmd(opts *Options) *cobra.Command {
	f := &tailFlags{}
	cmd := &cobra.Command{
		Use:   "tail <email-id>",
		Short: "Follow the event stream for one email (alias for `hail tail email:<id>`)",
		Long: `hail email tail — follow events for one email.

<email-id> may be a full UUID or a 4+ char hex prefix. See ` + "`hail tail --help`" + `
for stream flags (--from-start, --no-follow, --interval, --kind).`,
		Args: argsOrHelp(1, "<email-id>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runEmailTail(cmd.Context(), opts, f, args[0])
		},
	}
	registerTailFlags(cmd, f)
	return cmd
}

func runEmailTail(ctx context.Context, opts *Options, f *tailFlags, input string) error {
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}
	id, err := resolveEmailID(ctx, apiClient, input)
	if err != nil {
		return err
	}
	f.id = fmt.Sprintf("email:%s", id.String())
	return runTail(ctx, opts, f)
}
