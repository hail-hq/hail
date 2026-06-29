package cmd

import (
	"fmt"

	"github.com/spf13/cobra"
)

func newAuthTokenCmd(opts *Options) *cobra.Command {
	return &cobra.Command{
		Use:   "token",
		Short: "Print the bare API key (for scripting)",
		Long: `hail auth token — print the resolved API key to stdout.

Use for shell scripting:

  export HAIL_API_KEY=$(hail auth token)

Resolves --api-key > $HAIL_API_KEY > ~/.hail/credentials.json. Exits 2
with the standard not-authenticated hint if no key is configured.`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			if err := requireAuth(opts); err != nil {
				return err
			}
			fmt.Fprintln(opts.Stdout, opts.APIKey)
			return nil
		},
	}
}
