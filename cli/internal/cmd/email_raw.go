package cmd

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"os"

	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/hail-hq/hail/cli/internal/client"
)

// errorBodyLimit caps how much of a non-2xx response body we read into
// memory before forwarding to apiError. apiError displays at most 1024
// bytes itself; the extra headroom is just slack for a future format
// change. Keeps a hostile/buggy server from streaming gigabytes into RAM.
const errorBodyLimit = 8 * 1024

func newEmailRawCmd(opts *Options) *cobra.Command {
	var output string
	cmd := &cobra.Command{
		Use:   "raw <email-id>",
		Short: "Write the raw RFC 5322 source of one email",
		Long: `hail email raw — emit the original RFC 5322 source of an email.

By default writes to stdout (binary stream — pipe to formail / mhonarc /
a file). Use --output path.eml to write to disk; --output - is the same
as omitting --output.

<email-id> may be a full UUID or a 4+ char hex prefix.

--json is not supported (the output is a binary stream, not JSON).`,
		Args: argsOrHelp(1, "<email-id>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			if opts.JSON {
				return helpAndFail(cmd, "--json is not supported on raw — output is a binary stream")
			}
			return runEmailRaw(cmd.Context(), cmd, opts, args[0], output)
		},
	}
	cmd.Flags().StringVar(&output, "output", "", "Write to a file (use '-' or omit for stdout)")
	return cmd
}

func runEmailRaw(ctx context.Context, cmd *cobra.Command, opts *Options, input, output string) error {
	if (output == "" || output == "-") && isTTY(opts.Stdout) {
		return helpAndFail(cmd, "stdout is a TTY; pass --output <path> or pipe to a file")
	}
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}
	id, err := resolveEmailID(ctx, apiClient, input)
	if err != nil {
		return err
	}
	resp, err := apiClient.GetEmailRawV1EmailsEmailIdRawGet(
		ctx, openapi_types.UUID(id), &client.GetEmailRawV1EmailsEmailIdRawGetParams{},
	)
	if err != nil {
		return fmt.Errorf("email raw API: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, errorBodyLimit))
		return apiError(resp.StatusCode, body)
	}

	dst, closeDst, err := openOutput(opts, output)
	if err != nil {
		return err
	}
	if _, err := io.Copy(dst, resp.Body); err != nil {
		_ = closeDst()
		return fmt.Errorf("write raw: %w", err)
	}
	if err := closeDst(); err != nil {
		return fmt.Errorf("close %s: %w", output, err)
	}
	return nil
}

// openOutput resolves an --output flag value to a writer + a close
// function. Returns opts.Stdout when the flag is "" or "-" (close is a
// no-op), or opens the named file (truncating, mode 0644) and returns a
// closer that surfaces the file's Close error so deferred-write failures
// on disk are not silently treated as success.
func openOutput(opts *Options, path string) (io.Writer, func() error, error) {
	if path == "" || path == "-" {
		return opts.Stdout, func() error { return nil }, nil
	}
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o644)
	if err != nil {
		return nil, func() error { return nil }, fmt.Errorf("open %s: %w", path, err)
	}
	return f, f.Close, nil
}
