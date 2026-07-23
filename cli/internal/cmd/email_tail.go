package cmd

import (
	"context"

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
	// runTail owns prefix resolution (resolveTailID); this alias only pins
	// the resource type.
	f.id = "email:" + input
	return runTail(ctx, opts, f)
}
