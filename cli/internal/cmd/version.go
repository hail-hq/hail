package cmd

import (
	"fmt"

	"github.com/spf13/cobra"
)

func newVersionCmd(opts *Options) *cobra.Command {
	return &cobra.Command{
		Use:   "version",
		Short: "Print hail CLI version, commit, and build date",
		Long: `hail version — print the same string as hail --version.

JSON form (for scripting):
  hail version --json`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			if opts.JSON {
				return printJSON(opts.Stdout, map[string]string{
					"version": version,
					"commit":  commit,
					"built":   buildDate,
				})
			}
			fmt.Fprintf(opts.Stdout, "hail %s (commit %s, built %s)\n", version, commit, buildDate)
			return nil
		},
	}
}
