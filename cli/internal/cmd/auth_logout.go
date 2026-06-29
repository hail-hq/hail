// cli/internal/cmd/auth_logout.go
package cmd

import (
	"fmt"

	"github.com/spf13/cobra"
)

func newAuthLogoutCmd(opts *Options) *cobra.Command {
	return &cobra.Command{
		Use:   "logout",
		Short: "Remove the local Hail credentials file",
		Long: `hail auth logout — delete ~/.hail/credentials.json.

Idempotent. Does NOT revoke the API key server-side; revoke from the
console if you need to invalidate the key everywhere.`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			existed, err := deleteCredentials()
			if err != nil {
				return err
			}
			if existed {
				fmt.Fprintln(opts.Stdout, "Signed out.")
			} else {
				fmt.Fprintln(opts.Stdout, "Already signed out.")
			}
			return nil
		},
	}
}
