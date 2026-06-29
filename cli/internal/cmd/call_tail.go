package cmd

import (
	"context"
	"fmt"

	"github.com/spf13/cobra"
)

// newCallTailCmd is sugar for `hail tail call:<uuid>` that adds prefix
// resolution. Body is intentionally tiny — the polling loop lives in
// tail.go.
func newCallTailCmd(opts *Options) *cobra.Command {
	f := &tailFlags{}
	cmd := &cobra.Command{
		Use:   "tail <call-id>",
		Short: "Follow the event stream for one call (alias for `hail tail call:<id>`)",
		Long: `hail call tail — follow events for one call.

<call-id> may be a full UUID or a 4+ char hex prefix. See ` + "`hail tail --help`" + `
for stream flags (--from-start, --no-follow, --interval, --kind).`,
		Args: argsOrHelp(1, "<call-id>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runCallTail(cmd.Context(), opts, f, args[0])
		},
	}
	registerTailFlags(cmd, f)
	return cmd
}

func runCallTail(ctx context.Context, opts *Options, f *tailFlags, input string) error {
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}
	id, _, err := resolveCallID(ctx, apiClient, input)
	if err != nil {
		return err
	}
	f.id = fmt.Sprintf("call:%s", id.String())
	return runTail(ctx, opts, f)
}
