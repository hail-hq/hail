package cmd

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"strings"
	"text/tabwriter"

	"github.com/google/uuid"
	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/hail-hq/hail/cli/internal/client"
)

// newAgentsCmd builds the `agents` subtree.
//
// An agent is the saved brain a number answers with: instructions, greeting,
// AI line, voice, tools, limits. Numbers pick their agent with
// `hail numbers route`. Outbound calls with an agent go through the API,
// SDK or MCP (`agent_id`); `hail call` has no `--agent` flag yet.
func newAgentsCmd(opts *Options) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "agents",
		Short: "Manage agents that answer calls and texts on your numbers",
		Long: `hail agents — the saved brains your numbers answer with.

Create one, then point a number at it:
  hail agents create "Front desk" --prompt-file ./front-desk.md --first-message "How can I help?"
  hail numbers route <number-id> --calls <agent-id> --texts <agent-id>`,
	}
	cmd.AddCommand(newAgentsListCmd(opts))
	cmd.AddCommand(newAgentsGetCmd(opts))
	cmd.AddCommand(newAgentsCreateCmd(opts))
	cmd.AddCommand(newAgentsUpdateCmd(opts))
	cmd.AddCommand(newAgentsDeleteCmd(opts))
	return cmd
}

// --------------------------------------------------------------------------- //
// list / get
// --------------------------------------------------------------------------- //

func newAgentsListCmd(opts *Options) *cobra.Command {
	return &cobra.Command{
		Use:     "list",
		Aliases: []string{"ls"},
		Short:   "List the org's agents",
		Args:    cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			resp, err := apiClient.ListAgentsV1AgentsGetWithResponse(cmd.Context(), &client.ListAgentsV1AgentsGetParams{})
			if err != nil {
				return fmt.Errorf("agents API: %w", err)
			}
			if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
				return apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			return printAgentList(opts, resp.JSON200)
		},
	}
}

func newAgentsGetCmd(opts *Options) *cobra.Command {
	return &cobra.Command{
		Use:   "get <id>",
		Short: "Show one agent",
		Args:  argsOrHelp(1, "<id>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			id, err := uuid.Parse(args[0])
			if err != nil {
				return fmt.Errorf("agent id must be a UUID: %w", err)
			}
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			resp, err := apiClient.GetAgentV1AgentsAgentIdGetWithResponse(
				cmd.Context(), openapi_types.UUID(id), &client.GetAgentV1AgentsAgentIdGetParams{})
			if err != nil {
				return fmt.Errorf("agents API: %w", err)
			}
			if resp.HTTPResponse.StatusCode == http.StatusNotFound {
				return fmt.Errorf("agent %s not found (or not in your org)", id)
			}
			if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
				return apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			return printAgent(opts, resp.JSON200, false)
		},
	}
}

// --------------------------------------------------------------------------- //
// create / update
// --------------------------------------------------------------------------- //

// agentFlags are shared by create and update. update sends only the flags
// the user set (cobra's Changed), so a field left out keeps its value.
type agentFlags struct {
	prompt       string
	promptFile   string
	firstMessage string
	aiLine       string
	noAILine     bool
	voiceID      string
	language     string
	maxMinutes   int
	tools        []string
	noSms        bool
	noCalls      bool
	status       string
	name         string
}

func (f *agentFlags) bind(cmd *cobra.Command) {
	cmd.Flags().StringVar(&f.prompt, "prompt", "", "Task instructions (system prompt)")
	cmd.Flags().StringVar(&f.promptFile, "prompt-file", "", "Read the instructions from a file ('-' for stdin)")
	cmd.Flags().StringVar(&f.firstMessage, "first-message", "", "Opening line after the AI line ('' = wait for the other side)")
	cmd.Flags().StringVar(&f.aiLine, "ai-line", "", "AI line template; {org} becomes the organization name ('' = workspace line)")
	cmd.Flags().BoolVar(&f.noAILine, "no-ai-line", false, "Do not speak the AI line (you verified it is not required)")
	cmd.Flags().StringVar(&f.voiceID, "voice", "", "TTS voice id")
	cmd.Flags().StringVar(&f.language, "language", "", "Spoken language, ISO 639-1 (e.g. fr)")
	cmd.Flags().IntVar(&f.maxMinutes, "max-minutes", 0, "Soft cap per call, 1..60 minutes (0 = workspace limit)")
	cmd.Flags().StringSliceVar(&f.tools, "tools", nil, "Allowed tools, comma-separated ('' = none; omitted = all; on update, --all-tools resets to all)")
	cmd.Flags().BoolVar(&f.noSms, "no-sms", false, "Do not answer texts")
	cmd.Flags().BoolVar(&f.noCalls, "no-calls", false, "Do not answer calls")
	cmd.Flags().StringVar(&f.status, "status", "", "live or paused")
}

func (f *agentFlags) readPrompt() (string, bool, error) {
	if f.promptFile != "" {
		var data []byte
		var err error
		if f.promptFile == "-" {
			data, err = readAllStdin()
		} else {
			data, err = os.ReadFile(f.promptFile)
		}
		if err != nil {
			return "", false, fmt.Errorf("read --prompt-file: %w", err)
		}
		return strings.TrimSpace(string(data)), true, nil
	}
	if f.prompt != "" {
		return f.prompt, true, nil
	}
	return "", false, nil
}

func readAllStdin() ([]byte, error) {
	var buf bytes.Buffer
	_, err := buf.ReadFrom(os.Stdin)
	return buf.Bytes(), err
}

// body returns the JSON object for create (all set fields) or update (only
// flags the user changed). Built as a map so update can send explicit nulls.
//
// The API replaces voice_config as a whole, so on update the voice argument is the
// agent's current voice_config: --voice or --language alone must not drop the
// other setting (or the TTS provider and the rest of the stored config).
func (f *agentFlags) body(cmd *cobra.Command, create bool, voice map[string]any) (map[string]any, error) {
	// Both verbs send only what the user asked for: the API's defaults apply
	// on create, and untouched fields keep their value on update.
	b := map[string]any{}
	changed := func(name string) bool { return cmd.Flags().Changed(name) }

	prompt, has, err := f.readPrompt()
	if err != nil {
		return nil, err
	}
	if has {
		b["system_prompt"] = prompt
	} else if create {
		return nil, fmt.Errorf("--prompt or --prompt-file is required")
	}
	if f.name != "" {
		b["name"] = f.name
	}
	if changed("first-message") {
		b["first_message"] = nullIfEmpty(f.firstMessage)
	}
	if changed("ai-line") {
		b["ai_disclosure_line"] = nullIfEmpty(f.aiLine)
	}
	if changed("no-ai-line") {
		b["ai_disclosure"] = !f.noAILine
	}
	if changed("no-sms") {
		b["sms_enabled"] = !f.noSms
	}
	if changed("no-calls") {
		b["voice_enabled"] = !f.noCalls
	}
	if changed("status") && f.status != "" {
		b["status"] = f.status
	}
	if changed("max-minutes") {
		if f.maxMinutes == 0 {
			b["max_duration_seconds"] = nil
		} else {
			b["max_duration_seconds"] = f.maxMinutes * 60
		}
	}
	if changed("tools") {
		// pflag hands `--tools ''` over as an empty, non-nil slice: no tools.
		b["tools"] = f.tools
	}
	if changed("voice") || changed("language") {
		vc := map[string]any{}
		for k, v := range voice {
			vc[k] = v
		}
		setOrClear := func(key, value string) {
			if value == "" {
				delete(vc, key)
			} else {
				vc[key] = value
			}
		}
		if changed("voice") {
			setOrClear("voice_id", f.voiceID)
		}
		if changed("language") {
			setOrClear("language", f.language)
		}
		b["voice_config"] = vc
	}
	return b, nil
}

// currentVoiceConfig is the agent's stored voice_config, the base an update
// of --voice or --language is merged into.
func currentVoiceConfig(ctx context.Context, apiClient *client.ClientWithResponses, id uuid.UUID) (map[string]any, error) {
	resp, err := apiClient.GetAgentV1AgentsAgentIdGetWithResponse(
		ctx, openapi_types.UUID(id), &client.GetAgentV1AgentsAgentIdGetParams{})
	if err != nil {
		return nil, fmt.Errorf("agents API: %w", err)
	}
	if resp.HTTPResponse.StatusCode == http.StatusNotFound {
		return nil, fmt.Errorf("agent %s not found (or not in your org)", id)
	}
	if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
		return nil, apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}
	return resp.JSON200.VoiceConfig, nil
}

func nullIfEmpty(s string) any {
	if s == "" {
		return nil
	}
	return s
}

func newAgentsCreateCmd(opts *Options) *cobra.Command {
	f := &agentFlags{}
	cmd := &cobra.Command{
		Use:   "create <name>",
		Short: "Create an agent",
		Long: `hail agents create — save an agent.

Example:
  hail agents create "Front desk" --prompt-file ./front-desk.md \
    --first-message "Thanks for calling. How can I help?" --language en`,
		Args: argsOrHelp(1, "<name>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			f.name = args[0]
			body, err := f.body(cmd, true, nil)
			if err != nil {
				return err
			}
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			raw, _ := json.Marshal(body)
			resp, err := apiClient.CreateAgentV1AgentsPostWithBodyWithResponse(
				cmd.Context(), &client.CreateAgentV1AgentsPostParams{}, "application/json", bytes.NewReader(raw))
			if err != nil {
				return fmt.Errorf("agents API: %w", err)
			}
			if resp.HTTPResponse.StatusCode != http.StatusCreated || resp.JSON201 == nil {
				return apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			return printAgent(opts, resp.JSON201, true)
		},
	}
	f.bind(cmd)
	return cmd
}

func newAgentsUpdateCmd(opts *Options) *cobra.Command {
	f := &agentFlags{}
	var allTools bool
	cmd := &cobra.Command{
		Use:   "update <id>",
		Short: "Change an agent (only the flags you pass change)",
		Args:  argsOrHelp(1, "<id>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			id, err := uuid.Parse(args[0])
			if err != nil {
				return fmt.Errorf("agent id must be a UUID: %w", err)
			}
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			var voice map[string]any
			if cmd.Flags().Changed("voice") || cmd.Flags().Changed("language") {
				if voice, err = currentVoiceConfig(cmd.Context(), apiClient, id); err != nil {
					return err
				}
			}
			body, err := f.body(cmd, false, voice)
			if err != nil {
				return err
			}
			if allTools {
				body["tools"] = nil // null: every tool the channels support
			}
			if len(body) == 0 {
				return fmt.Errorf("nothing to change: pass at least one flag")
			}
			raw, _ := json.Marshal(body)
			resp, err := apiClient.UpdateAgentV1AgentsAgentIdPatchWithBodyWithResponse(
				cmd.Context(), openapi_types.UUID(id), &client.UpdateAgentV1AgentsAgentIdPatchParams{},
				"application/json", bytes.NewReader(raw))
			if err != nil {
				return fmt.Errorf("agents API: %w", err)
			}
			if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
				return apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			return printAgent(opts, resp.JSON200, true)
		},
	}
	f.bind(cmd)
	cmd.Flags().StringVar(&f.name, "name", "", "New display name")
	cmd.Flags().BoolVar(&allTools, "all-tools", false, "Allow every tool again (clears --tools)")
	cmd.MarkFlagsMutuallyExclusive("tools", "all-tools")
	return cmd
}

func newAgentsDeleteCmd(opts *Options) *cobra.Command {
	return &cobra.Command{
		Use:   "delete <id>",
		Short: "Delete an agent; its numbers ring out again",
		Args:  argsOrHelp(1, "<id>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			id, err := uuid.Parse(args[0])
			if err != nil {
				return fmt.Errorf("agent id must be a UUID: %w", err)
			}
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			resp, err := apiClient.DeleteAgentV1AgentsAgentIdDeleteWithResponse(
				cmd.Context(), openapi_types.UUID(id), &client.DeleteAgentV1AgentsAgentIdDeleteParams{})
			if err != nil {
				return fmt.Errorf("agents API: %w", err)
			}
			if resp.HTTPResponse.StatusCode != http.StatusNoContent {
				return apiError(resp.HTTPResponse.StatusCode, resp.Body)
			}
			if !opts.JSON {
				fmt.Fprintf(opts.Stdout, "✓ Agent %s deleted\n", id)
			}
			return nil
		},
	}
}

// --------------------------------------------------------------------------- //
// numbers route
// --------------------------------------------------------------------------- //

type numberRouteFlags struct {
	calls string
	texts string
}

// newNumberRouteCmd hangs off `hail numbers`: which agent answers.
func newNumberRouteCmd(opts *Options) *cobra.Command {
	f := &numberRouteFlags{}
	cmd := &cobra.Command{
		Use:   "route <id>",
		Short: "Choose the agent that answers calls and texts on a number",
		Long: `hail numbers route — point a number at an agent.

--calls <agent-id> registers the number for inbound calls at the carrier and
on Hail's LiveKit inbound trunk; --calls none unregisters it (calls ring
out). --texts <agent-id> makes the agent answer texts; --texts none sends
texts to your webhooks only. A flag left out keeps its value.`,
		Args: argsOrHelp(1, "<id>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			if !cmd.Flags().Changed("calls") && !cmd.Flags().Changed("texts") {
				return fmt.Errorf("pass --calls and/or --texts")
			}
			apiClient, err := opts.newClient()
			if err != nil {
				return err
			}
			id, err := resolveNumberID(cmd.Context(), apiClient, args[0])
			if err != nil {
				return err
			}
			body := map[string]any{}
			if cmd.Flags().Changed("calls") {
				body["voice_agent_id"], err = agentRef(f.calls)
				if err != nil {
					return err
				}
			}
			if cmd.Flags().Changed("texts") {
				body["sms_agent_id"], err = agentRef(f.texts)
				if err != nil {
					return err
				}
			}
			return runNumberRoute(cmd.Context(), opts, id, body)
		},
	}
	cmd.Flags().StringVar(&f.calls, "calls", "", "Agent id that answers calls, or 'none'")
	cmd.Flags().StringVar(&f.texts, "texts", "", "Agent id that answers texts, or 'none'")
	return cmd
}

func agentRef(s string) (any, error) {
	if s == "" || strings.EqualFold(s, "none") {
		return nil, nil
	}
	id, err := uuid.Parse(s)
	if err != nil {
		return nil, fmt.Errorf("agent id must be a UUID or 'none': %w", err)
	}
	return id.String(), nil
}

func runNumberRoute(ctx context.Context, opts *Options, id uuid.UUID, body map[string]any) error {
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}
	raw, _ := json.Marshal(body)
	resp, err := apiClient.RouteNumberV1NumbersNumberIdPatchWithBodyWithResponse(
		ctx, openapi_types.UUID(id), &client.RouteNumberV1NumbersNumberIdPatchParams{},
		"application/json", bytes.NewReader(raw))
	if err != nil {
		return fmt.Errorf("numbers API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
		return apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}
	return printPhoneNumber(opts, resp.JSON200, true)
}

// --------------------------------------------------------------------------- //
// printers
// --------------------------------------------------------------------------- //

func printAgent(opts *Options, a *client.AgentResponse, banner bool) error {
	if opts.JSON {
		return printJSON(opts.Stdout, a)
	}
	if banner {
		fmt.Fprintf(opts.Stdout, "✓ Agent %s: %s\n", a.Name, a.Id.String())
	} else {
		fmt.Fprintf(opts.Stdout, "Agent %s: %s\n", a.Name, a.Id.String())
	}
	fmt.Fprintf(opts.Stdout, "  Status:        %s\n", a.Status)
	fmt.Fprintf(opts.Stdout, "  AI line:       %s\n", describeAILine(a))
	if a.FirstMessage != nil && *a.FirstMessage != "" {
		fmt.Fprintf(opts.Stdout, "  First message: %s\n", *a.FirstMessage)
	} else {
		fmt.Fprintf(opts.Stdout, "  First message: (waits for the other side)\n")
	}
	fmt.Fprintf(opts.Stdout, "  Calls:         %s\n", yesNo(a.VoiceEnabled))
	fmt.Fprintf(opts.Stdout, "  Texts:         %s\n", yesNo(a.SmsEnabled))
	if a.MaxDurationSeconds != nil {
		fmt.Fprintf(opts.Stdout, "  Max length:    %d min\n", *a.MaxDurationSeconds/60)
	}
	if a.Tools != nil {
		fmt.Fprintf(opts.Stdout, "  Tools:         %s\n", strings.Join(*a.Tools, ", "))
	}
	fmt.Fprintf(opts.Stdout, "  Instructions:\n")
	for _, line := range strings.Split(a.SystemPrompt, "\n") {
		fmt.Fprintf(opts.Stdout, "    %s\n", line)
	}
	return nil
}

func describeAILine(a *client.AgentResponse) string {
	if !a.AiDisclosure {
		return "off"
	}
	if a.AiDisclosureLine != nil && *a.AiDisclosureLine != "" {
		return *a.AiDisclosureLine
	}
	return "workspace default"
}

func yesNo(b bool) string {
	if b {
		return "yes"
	}
	return "no"
}

func printAgentList(opts *Options, body *client.AgentListResponse) error {
	if opts.JSON {
		return printJSON(opts.Stdout, body)
	}
	if len(body.Items) == 0 {
		fmt.Fprintln(opts.Stdout, "(no agents)")
		return nil
	}
	w := tabwriter.NewWriter(opts.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(w, "ID\tNAME\tSTATUS\tTEXTS\tAI LINE")
	for _, a := range body.Items {
		fmt.Fprintf(w, "%s\t%s\t%s\t%s\t%s\n", a.Id.String(), a.Name, a.Status, yesNo(a.SmsEnabled), describeAILine(&a))
	}
	return w.Flush()
}
