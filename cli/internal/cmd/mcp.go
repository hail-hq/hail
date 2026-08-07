package cmd

import (
	"fmt"
	"net/url"
	"strings"

	"github.com/spf13/cobra"
)

func newMcpCmd(opts *Options) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "mcp",
		Short: "MCP-related subcommands",
		Long:  `hail mcp — MCP-related subcommands.`,
	}
	cmd.AddCommand(newMcpEndpointCmd(opts))
	return cmd
}

func newMcpEndpointCmd(opts *Options) *cobra.Command {
	return &cobra.Command{
		Use:   "endpoint",
		Short: "Print the MCP server's Streamable HTTP URL",
		Long: `hail mcp endpoint — print the MCP server URL.

The Streamable HTTP transport serves the MCP root path; there is no
` + "`/mcp`" + ` suffix and no SSE. See docs/public/mcp.md.

Resolution order:
  $HAIL_MCP_URL                              (explicit override)
  api.<host>.<tld> → mcp.<host>.<tld>        (cloud convention)
  <api-url>                                  (self-host fallback)`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			endpoint := resolveMcpEndpoint(opts)
			if opts.JSON {
				return printJSON(opts.Stdout, map[string]string{
					"url":       endpoint,
					"transport": "streamable-http",
				})
			}
			fmt.Fprintln(opts.Stdout, endpoint)
			return nil
		},
	}
}

// resolveMcpEndpoint derives the MCP URL from configured sources.
// Order: HAIL_MCP_URL > api.<rest> → mcp.<rest> > the API URL itself.
// The Streamable HTTP transport serves at the root path (no /mcp suffix);
// see docs/public/mcp.md.
func resolveMcpEndpoint(opts *Options) string {
	if v := opts.Getenv("HAIL_MCP_URL"); v != "" {
		return v
	}
	apiURL := opts.ResolvedAPIURL()
	if u, err := url.Parse(apiURL); err == nil && strings.HasPrefix(u.Host, "api.") {
		u.Host = "mcp." + strings.TrimPrefix(u.Host, "api.")
		u.Path = ""
		return u.String()
	}
	return apiURL
}
