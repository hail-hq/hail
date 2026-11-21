package cmd

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"text/tabwriter"
	"time"

	"github.com/google/uuid"
	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/hail-hq/hail/cli/internal/client"
)

// newKnowledgeCmd builds `hail knowledge`: the documents saved agents
// answer calls, texts and email from (agents need --knowledge). The
// knowledge base is off until Hail support turns it on for the workspace;
// every command then returns the API's 403 message.
func newKnowledgeCmd(opts *Options) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "knowledge",
		Short: "Manage the knowledge base agents answer from",
		Long: `hail knowledge — documents your agents answer calls, texts and email from.

Add a PDF (with text, not a scan), DOCX, TXT, Markdown or CSV file. Hail
reads it in the background: 'pending', then 'ready' or 'failed'. Turn an
agent on with 'hail agents update <id> --knowledge'.

Example:
  hail knowledge add manual.pdf
  hail knowledge search "error E-204"`,
	}
	cmd.AddCommand(newKnowledgeAddCmd(opts))
	cmd.AddCommand(newKnowledgeListCmd(opts))
	cmd.AddCommand(newKnowledgeGetCmd(opts))
	cmd.AddCommand(newKnowledgeDeleteCmd(opts))
	cmd.AddCommand(newKnowledgeSearchCmd(opts))
	return cmd
}

// knowledgeTypes maps a file extension to the MIME type the API reads.
var knowledgeTypes = map[string]string{
	".pdf":      "application/pdf",
	".docx":     "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
	".txt":      "text/plain",
	".md":       "text/markdown",
	".markdown": "text/markdown",
	".csv":      "text/csv",
}

type knowledgeAddFlags struct {
	name        string
	contentType string
}

func newKnowledgeAddCmd(opts *Options) *cobra.Command {
	f := &knowledgeAddFlags{}
	cmd := &cobra.Command{
		Use:   "add <file>",
		Short: "Upload a file and add it to the knowledge base",
		Args:  argsOrHelp(1, "<file>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runKnowledgeAdd(cmd.Context(), opts, f, args[0])
		},
	}
	cmd.Flags().StringVar(&f.name, "name", "", "Display name (default: the file name)")
	cmd.Flags().StringVar(&f.contentType, "content-type", "", "MIME type (default: from the extension: .pdf .docx .txt .md .csv)")
	return cmd
}

func runKnowledgeAdd(ctx context.Context, opts *Options, f *knowledgeAddFlags, path string) error {
	contentType := f.contentType
	if contentType == "" {
		contentType = knowledgeTypes[strings.ToLower(filepath.Ext(path))]
		if contentType == "" {
			return fmt.Errorf("cannot tell the type of %s; pass --content-type", path)
		}
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("read %s: %w", path, err)
	}
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}
	link, err := apiClient.CreateFileUploadWithResponse(ctx, &client.CreateFileUploadParams{}, client.FileUploadCreate{
		Purpose:     client.FileUploadCreatePurposeKnowledgeDocument,
		Filename:    filepath.Base(path),
		ContentType: contentType,
	})
	if err != nil {
		return fmt.Errorf("uploads API: %w", err)
	}
	if link.HTTPResponse.StatusCode != http.StatusCreated || link.JSON201 == nil {
		return apiError(link.HTTPResponse.StatusCode, link.Body)
	}
	if len(data) > link.JSON201.MaxBytes {
		return fmt.Errorf("%s is %d bytes; the workspace takes files up to %d bytes", path, len(data), link.JSON201.MaxBytes)
	}
	if err := putUpload(ctx, link.JSON201.UploadUrl, data); err != nil {
		return err
	}
	body := client.KnowledgeDocumentCreate{UploadId: link.JSON201.Id, Name: strPtr(f.name)}
	resp, err := apiClient.CreateKnowledgeDocumentWithResponse(ctx, &client.CreateKnowledgeDocumentParams{}, body)
	if err != nil {
		return fmt.Errorf("knowledge API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusCreated || resp.JSON201 == nil {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}
	return printKnowledgeDocument(opts, resp.JSON201, "added")
}

// putUpload sends the bytes to a one-time upload link. The link's token is
// its only credential: no Authorization header goes with it.
func putUpload(ctx context.Context, url string, data []byte) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodPut, url, bytes.NewReader(data))
	if err != nil {
		return fmt.Errorf("upload: %w", err)
	}
	req.Header.Set("Content-Type", "application/octet-stream")
	resp, err := (&http.Client{Timeout: 10 * time.Minute}).Do(req)
	if err != nil {
		return fmt.Errorf("upload: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return apiError(resp.StatusCode, body)
	}
	return nil
}

type knowledgeListFlags struct {
	limit  int
	cursor string
	all    bool
}

func newKnowledgeListCmd(opts *Options) *cobra.Command {
	f := &knowledgeListFlags{}
	cmd := &cobra.Command{
		Use:     "list",
		Aliases: []string{"ls"},
		Short:   "List knowledge documents, newest first",
		Args:    cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			items, next, err := walkCursor(f.all, f.cursor, opts.Stderr, "documents",
				func(cursor string) (cursorPage[client.KnowledgeDocumentResponse], error) {
					params := &client.ListKnowledgeDocumentsParams{Cursor: strPtr(cursor), Limit: &f.limit}
					resp, err := apiClient.ListKnowledgeDocumentsWithResponse(cmd.Context(), params)
					if err != nil {
						return cursorPage[client.KnowledgeDocumentResponse]{}, fmt.Errorf("knowledge API: %w", err)
					}
					if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
						return cursorPage[client.KnowledgeDocumentResponse]{}, apiError(resp.HTTPResponse.StatusCode, resp.Body)
					}
					return cursorPage[client.KnowledgeDocumentResponse]{items: resp.JSON200.Items, nextCursor: resp.JSON200.NextCursor}, nil
				})
			if err != nil {
				return err
			}
			if opts.JSON {
				return printJSON(opts.Stdout, client.KnowledgeDocumentListResponse{Items: items, NextCursor: next})
			}
			w := tabwriter.NewWriter(opts.Stdout, 0, 4, 2, ' ', 0)
			fmt.Fprintln(w, "ID\tNAME\tSTATUS\tPIECES\tSIZE")
			for _, d := range items {
				fmt.Fprintf(w, "%s\t%s\t%s\t%d\t%d\n", d.Id.String(), d.Name, d.Status, d.PieceCount, d.SizeBytes)
			}
			if err := w.Flush(); err != nil {
				return err
			}
			if next != nil && *next != "" {
				fmt.Fprintf(opts.Stderr, "More: --cursor %s\n", *next)
			}
			return nil
		},
	}
	cmd.Flags().IntVar(&f.limit, "limit", 50, "Page size (1..100)")
	cmd.Flags().StringVar(&f.cursor, "cursor", "", "Resume from a previous next_cursor")
	cmd.Flags().BoolVar(&f.all, "all", false, "Walk every page")
	return cmd
}

func newKnowledgeGetCmd(opts *Options) *cobra.Command {
	return &cobra.Command{
		Use:   "get <id>",
		Short: "Show one knowledge document and its status",
		Args:  argsOrHelp(1, "<id>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			id, err := uuid.Parse(args[0])
			if err != nil {
				return fmt.Errorf("document id must be a UUID: %w", err)
			}
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			resp, err := apiClient.GetKnowledgeDocumentWithResponse(
				cmd.Context(), openapi_types.UUID(id), &client.GetKnowledgeDocumentParams{})
			if err != nil {
				return fmt.Errorf("knowledge API: %w", err)
			}
			if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
				return apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			return printKnowledgeDocument(opts, resp.JSON200, "")
		},
	}
}

func newKnowledgeDeleteCmd(opts *Options) *cobra.Command {
	return &cobra.Command{
		Use:   "delete <id>",
		Short: "Delete a knowledge document, its file and its pieces",
		Args:  argsOrHelp(1, "<id>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			id, err := uuid.Parse(args[0])
			if err != nil {
				return fmt.Errorf("document id must be a UUID: %w", err)
			}
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			resp, err := apiClient.DeleteKnowledgeDocumentWithResponse(
				cmd.Context(), openapi_types.UUID(id), &client.DeleteKnowledgeDocumentParams{})
			if err != nil {
				return fmt.Errorf("knowledge API: %w", err)
			}
			if resp.HTTPResponse.StatusCode != http.StatusNoContent {
				return apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			if !opts.JSON {
				fmt.Fprintf(opts.Stdout, "✓ Document %s deleted\n", id)
			}
			return nil
		},
	}
}

func newKnowledgeSearchCmd(opts *Options) *cobra.Command {
	var limit int
	cmd := &cobra.Command{
		Use:   "search <query>",
		Short: "Search the knowledge base the way agents do",
		Args:  argsOrHelp(1, "<query>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			resp, err := apiClient.SearchKnowledgeWithResponse(cmd.Context(), &client.SearchKnowledgeParams{},
				client.KnowledgeSearchRequest{Query: args[0], Limit: &limit})
			if err != nil {
				return fmt.Errorf("knowledge API: %w", err)
			}
			if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
				return apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			if opts.JSON {
				return printJSON(opts.Stdout, resp.JSON200)
			}
			if len(resp.JSON200.Results) == 0 {
				fmt.Fprintln(opts.Stdout, "No match.")
			}
			for i, r := range resp.JSON200.Results {
				where := ""
				if r.Page != nil {
					where = fmt.Sprintf(", page %d", *r.Page)
				} else if r.Row != nil {
					where = fmt.Sprintf(", row %d", *r.Row)
				}
				fmt.Fprintf(opts.Stdout, "%d. %s%s\n%s\n\n", i+1, r.DocumentName, where, r.Text)
			}
			return nil
		},
	}
	cmd.Flags().IntVar(&limit, "limit", 5, "Pieces to return (1..10)")
	return cmd
}

func printKnowledgeDocument(opts *Options, d *client.KnowledgeDocumentResponse, verb string) error {
	if opts.JSON {
		return printJSON(opts.Stdout, d)
	}
	if verb != "" {
		fmt.Fprintf(opts.Stdout, "✓ Document %s: %s\n", verb, d.Id.String())
	} else {
		fmt.Fprintf(opts.Stdout, "Document %s\n", d.Id.String())
	}
	fmt.Fprintf(opts.Stdout, "  Name:    %s\n", d.Name)
	fmt.Fprintf(opts.Stdout, "  Status:  %s\n", d.Status)
	fmt.Fprintf(opts.Stdout, "  Pieces:  %d\n", d.PieceCount)
	if d.Error != nil {
		fmt.Fprintf(opts.Stdout, "  Error:   %s\n", *d.Error)
	}
	if d.Status == client.KnowledgeDocumentResponseStatusPending || d.Status == client.KnowledgeDocumentResponseStatusProcessing {
		fmt.Fprintf(opts.Stdout, "  Check:   hail knowledge get %s\n", d.Id.String())
	}
	return nil
}
