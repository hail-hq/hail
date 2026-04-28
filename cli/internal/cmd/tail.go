package cmd

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"hash/fnv"
	"io"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/google/uuid"
	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/hail-hq/hail/cli/internal/client"
)

// errInterrupted is returned by the tail loop when the user pressed Ctrl-C
// (SIGINT). main.Execute recognizes it and exits 130 without printing.
var errInterrupted = errors.New("interrupted")

// encodeEventCursor mirrors the API's _encode_event_cursor: a urlsafe-b64
// (no padding) of "<isoformat>|<uuid>". The Python decoder uses
// datetime.fromisoformat which accepts both `Z` and `+00:00` suffixes on
// 3.11+, so RFC3339Nano (Z) round-trips cleanly.
func encodeEventCursor(occurredAt time.Time, eventID openapi_types.UUID) string {
	raw := fmt.Sprintf("%s|%s", occurredAt.UTC().Format(time.RFC3339Nano), uuid.UUID(eventID).String())
	return base64.RawURLEncoding.EncodeToString([]byte(raw))
}

type tailFlags struct {
	id         string
	kind       string
	intervalMS int
	fromStart  bool
	noFollow   bool
}

// supportedResourceTypes mirrors core.schemas.SUPPORTED_RESOURCE_TYPES — kept
// in lockstep so `hail tail --id sms:...` fails fast on the CLI in v1
// without an HTTP round-trip. When a new channel lands on the API, add it
// here in the same change.
var supportedResourceTypes = []string{"call"}

// terminalCallStatuses are the values from the spec that mean "no more
// events will arrive" — when `--id call:<uuid>` is set and the server
// reports any of these, the tail loop exits cleanly.
var terminalCallStatuses = map[client.EventStreamResponseCallStatus]bool{
	client.EventStreamResponseCallStatusCompleted: true,
	client.EventStreamResponseCallStatusFailed:    true,
	client.EventStreamResponseCallStatusBusy:      true,
	client.EventStreamResponseCallStatusNoAnswer:  true,
	client.EventStreamResponseCallStatusCanceled:  true,
}

// parseResourceID mirrors core.schemas.parse_resource_id: validate and split
// a "<type>:<uuid>" value before any HTTP. Errors carry the same shape as
// the API helper but without brackets (matches the CLI error contract).
func parseResourceID(value string) (resType string, resID uuid.UUID, err error) {
	idx := strings.Index(value, ":")
	if idx < 0 {
		return "", uuid.Nil, fmt.Errorf("must be '<type>:<uuid>' (e.g. 'call:abc-...'); missing ':'")
	}
	resType = value[:idx]
	idStr := value[idx+1:]
	if resType == "" {
		return "", uuid.Nil, fmt.Errorf("missing resource type before ':'")
	}
	if idStr == "" {
		return "", uuid.Nil, fmt.Errorf("missing resource id after ':'")
	}
	supported := false
	for _, t := range supportedResourceTypes {
		if t == resType {
			supported = true
			break
		}
	}
	if !supported {
		return "", uuid.Nil, fmt.Errorf(
			"unsupported resource type %q; supported: %s",
			resType, strings.Join(supportedResourceTypes, ", "),
		)
	}
	parsed, perr := uuid.Parse(idStr)
	if perr != nil {
		return "", uuid.Nil, fmt.Errorf("invalid uuid %q: %w", idStr, perr)
	}
	return resType, parsed, nil
}

// ANSI color codes used when stdout is a TTY and NO_COLOR is unset.
const (
	colorReset   = "\x1b[0m"
	colorCyan    = "\x1b[36m"
	colorYellow  = "\x1b[33m"
	colorMagenta = "\x1b[35m"
	colorGreen   = "\x1b[32m"
	colorBlue    = "\x1b[34m"
	colorRed     = "\x1b[31m"
	colorDim     = "\x1b[2m"
)

// perCallPalette is the small set of stable colors assigned to short call
// ids in org-wide tail mode. Hashed lookup lives in shortIDColor.
var perCallPalette = []string{
	colorCyan,
	colorYellow,
	colorMagenta,
	colorGreen,
	colorBlue,
	colorRed,
}

func newTailCmd(opts *Options) *cobra.Command {
	f := &tailFlags{}
	cmd := &cobra.Command{
		Use:   "tail",
		Short: "Stream events from across the org (or one resource with --id)",
		Long: `hail tail — follow the event stream

Without --id, tail follows every event in the org, prefixing each line
with a short call id ([c2a8f1d3]) so multiple in-flight conversations
disambiguate at a glance. Runs until Ctrl-C.

With --id call:<uuid>, tail narrows to a single call and auto-exits when
the call reaches a terminal status (completed/failed/busy/no_answer/
canceled). The colon-form mirrors the audit_log resource_type/resource_id
shape so SMS / email / conversation can join later without a rename.`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runTail(cmd.Context(), opts, f)
		},
	}
	cmd.Flags().StringVar(&f.id, "id", "", "Narrow to one resource as '<type>:<uuid>' (e.g. 'call:abc-...'); v1 supports: call")
	cmd.Flags().StringVar(&f.kind, "kind", "", "Filter by event kind (server-side, exact match)")
	cmd.Flags().IntVar(&f.intervalMS, "interval", 500, "Poll interval in ms (100..10000)")
	cmd.Flags().BoolVar(&f.fromStart, "from-start", false, "Fetch all historical events first (default: start from now)")
	cmd.Flags().BoolVar(&f.noFollow, "no-follow", false, "Print one page and exit (no follow)")
	return cmd
}

func runTail(ctx context.Context, opts *Options, f *tailFlags) error {
	if f.intervalMS < 100 || f.intervalMS > 10000 {
		return fmt.Errorf("--interval must be in [100, 10000] ms, got %d", f.intervalMS)
	}

	// Parse --id early so malformed / unsupported values fail before any
	// network IO. The CLI knows the supported set in v1; it does not have to
	// round-trip the API to validate.
	var (
		idWire       string // exact "<type>:<uuid>" string put on the wire
		resourceType string
	)
	if f.id != "" {
		rtype, rid, err := parseResourceID(f.id)
		if err != nil {
			return err
		}
		resourceType = rtype
		idWire = fmt.Sprintf("%s:%s", rtype, rid.String())
	}
	// In v1 the only supported type is `call`; this also drives whether the
	// renderer omits the short-id prefix and whether the loop watches for a
	// terminal `call_status`.
	singleCall := resourceType == "call"

	// SIGINT cancels the poll loop. Exit 130 happens at Execute() — we just
	// return errInterrupted from here.
	tailCtx, stop := signal.NotifyContext(ctx, os.Interrupt, syscall.SIGTERM)
	defer stop()

	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}

	colorize := shouldColorize(opts.Stdout)

	// Default starting point: "now" — synthesize a cursor from the current
	// time so the server filters historical events server-side. uuid.Nil is
	// the minimum UUID; the server uses strict `>` on (occurred_at, id), so
	// any event at or after startTime is admitted. --from-start sends no
	// cursor and walks the full history instead.
	var cursor string
	if !f.fromStart {
		cursor = encodeEventCursor(time.Now().UTC(), openapi_types.UUID(uuid.Nil))
	}
	interval := time.Duration(f.intervalMS) * time.Millisecond

	for {
		params := &client.ListEventsEventsGetParams{}
		if cursor != "" {
			c := cursor
			params.Cursor = &c
		}
		// Generous limit so a single fetch can drain a long backlog.
		limit := 1000
		params.Limit = &limit
		if idWire != "" {
			v := idWire
			params.Id = &v
		}
		if f.kind != "" {
			k := f.kind
			params.Kind = &k
		}

		resp, err := apiClient.ListEventsEventsGetWithResponse(tailCtx, params)
		if err != nil {
			if tailCtx.Err() != nil {
				return errInterrupted
			}
			return fmt.Errorf("poll events: %w", err)
		}
		if resp.HTTPResponse.StatusCode == http.StatusNotFound {
			return fmt.Errorf("%s %s not found (or not in your org)", resourceType, f.id)
		}
		if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
			return apiErrorGeneric(resp.HTTPResponse.StatusCode, resp.Body)
		}

		// Drain inner pages — the server only sets next_cursor when more rows
		// exist beyond `limit`. In steady-state polling we drain everything in
		// one fetch; we synthesize the next polling cursor from the last seen
		// event so the next poll picks up after the events we just printed.
		page := resp.JSON200
		var lastEvent *client.CallEventResponse
		for {
			for i := range page.Items {
				if err := renderEvent(opts, page.Items[i], singleCall, colorize); err != nil {
					return err
				}
				lastEvent = &page.Items[i]
			}
			if page.NextCursor == nil || *page.NextCursor == "" {
				break
			}
			cursor = *page.NextCursor
			c := cursor
			subParams := &client.ListEventsEventsGetParams{Cursor: &c, Limit: &limit}
			if idWire != "" {
				v := idWire
				subParams.Id = &v
			}
			if f.kind != "" {
				k := f.kind
				subParams.Kind = &k
			}
			subResp, err := apiClient.ListEventsEventsGetWithResponse(tailCtx, subParams)
			if err != nil {
				if tailCtx.Err() != nil {
					return errInterrupted
				}
				return fmt.Errorf("poll events: %w", err)
			}
			if subResp.HTTPResponse.StatusCode != http.StatusOK || subResp.JSON200 == nil {
				return apiErrorGeneric(subResp.HTTPResponse.StatusCode, subResp.Body)
			}
			page = subResp.JSON200
		}
		// Synthesize the forward cursor when the server didn't hand one back.
		if lastEvent != nil {
			cursor = encodeEventCursor(lastEvent.OccurredAt, lastEvent.Id)
		}

		if f.noFollow {
			return nil
		}

		// Auto-exit only when narrowed to a single call (--id call:<uuid>).
		// Org-wide tail and non-call resource types run until SIGINT.
		if singleCall && resp.JSON200.CallStatus != nil &&
			terminalCallStatuses[*resp.JSON200.CallStatus] {
			finalLine := fmt.Sprintf("call %s", string(*resp.JSON200.CallStatus))
			renderSystemLine(opts, time.Now().UTC(), finalLine, colorize)
			return nil
		}

		select {
		case <-tailCtx.Done():
			return errInterrupted
		case <-time.After(interval):
		}
	}
}

// renderEvent dispatches on event.Kind and writes one line to opts.Stdout.
// In --json mode each event is emitted as a single JSON object per line.
//
// `singleCall` is true when --call narrowed the stream; the short-call-id
// prefix is omitted in that mode (every event belongs to the same call,
// the prefix would be redundant noise).
func renderEvent(opts *Options, ev client.CallEventResponse, singleCall, colorize bool) error {
	if opts.JSON {
		out, err := json.Marshal(ev)
		if err != nil {
			return fmt.Errorf("encode event JSON: %w", err)
		}
		fmt.Fprintln(opts.Stdout, string(out))
		return nil
	}

	ts := ev.OccurredAt.UTC().Format("15:04:05")
	label, body := renderEventBody(ev)
	if colorize {
		label = colorFor(ev.Kind) + label + colorReset
	}
	if singleCall {
		fmt.Fprintf(opts.Stdout, "[%s] %-9s %s\n", ts, label, body)
		return nil
	}
	short := shortCallID(ev.CallId)
	prefix := fmt.Sprintf("[%s]", short)
	if colorize {
		prefix = shortIDColor(short) + prefix + colorReset
	}
	fmt.Fprintf(opts.Stdout, "[%s] %s %-9s %s\n", ts, prefix, label, body)
	return nil
}

// shortCallID returns the first 8 hex chars of the UUID (no dashes
// truncated — UUIDs render with dashes at the 8/4/4/4/12 boundary, so the
// first 8 chars come from the first dash-delimited group cleanly).
func shortCallID(id openapi_types.UUID) string {
	s := uuid.UUID(id).String()
	if len(s) >= 8 {
		return s[:8]
	}
	return s
}

// shortIDColor returns a stable color from perCallPalette by hashing the id
// — same call always gets the same color across polls.
func shortIDColor(short string) string {
	h := fnv.New32a()
	_, _ = h.Write([]byte(short))
	return perCallPalette[int(h.Sum32())%len(perCallPalette)]
}

// renderEventBody produces the (label, body) pair for a single event. The
// label is bracketed (e.g. "[agent]") and the body is the human-readable
// message. Always returns a non-crashing fallback even on missing fields.
func renderEventBody(ev client.CallEventResponse) (label, body string) {
	switch ev.Kind {
	case "state_change":
		from, _ := ev.Payload["from"].(string)
		to, _ := ev.Payload["to"].(string)
		if from == "" && to == "" {
			return "[system]", payloadJSON(ev.Payload)
		}
		return "[system]", fmt.Sprintf("%s → %s", from, to)
	case "agent_turn":
		text, _ := ev.Payload["text"].(string)
		if text == "" {
			return "[agent]", payloadJSON(ev.Payload)
		}
		return "[agent]", text
	case "user_turn":
		text, _ := ev.Payload["text"].(string)
		if text == "" {
			return "[user]", payloadJSON(ev.Payload)
		}
		return "[user]", text
	case "tool_call":
		// Verified shape (voicebot/.../agent.py): {"tools": [<name>, ...]}.
		// Spec mentions {"name", "args"} too — handle both defensively.
		if name, ok := ev.Payload["name"].(string); ok && name != "" {
			args, _ := json.Marshal(ev.Payload["args"])
			return "[tool]", fmt.Sprintf("%s(%s)", name, string(args))
		}
		if tools, ok := ev.Payload["tools"].([]interface{}); ok && len(tools) > 0 {
			names := make([]string, 0, len(tools))
			for _, t := range tools {
				if s, ok := t.(string); ok {
					names = append(names, s)
				}
			}
			if len(names) > 0 {
				return "[tool]", strings.Join(names, ", ")
			}
		}
		return "[tool]", payloadJSON(ev.Payload)
	case "error":
		if msg, ok := ev.Payload["error"].(string); ok && msg != "" {
			return "[error]", msg
		}
		if msg, ok := ev.Payload["detail"].(string); ok && msg != "" {
			return "[error]", msg
		}
		return "[error]", payloadJSON(ev.Payload)
	default:
		// Unknown kinds get rendered with their kind as the label so a future
		// event type doesn't need a CLI release to show up legibly.
		return "[" + ev.Kind + "]", payloadJSON(ev.Payload)
	}
}

func payloadJSON(p map[string]interface{}) string {
	b, err := json.Marshal(p)
	if err != nil {
		return fmt.Sprintf("%v", p)
	}
	return string(b)
}

// renderSystemLine emits a synthetic [system] line for the final
// "call <status>" notice on terminal status. JSON output mode skips it
// entirely — synthetic events would pollute the NDJSON stream.
func renderSystemLine(opts *Options, ts time.Time, msg string, colorize bool) {
	if opts.JSON {
		return
	}
	label := "[system]"
	if colorize {
		label = colorDim + label + colorReset
	}
	fmt.Fprintf(opts.Stdout, "[%s] %-9s %s\n", ts.Format("15:04:05"), label, msg)
}

func colorFor(kind string) string {
	switch kind {
	case "agent_turn":
		return colorCyan
	case "user_turn":
		return colorYellow
	case "state_change":
		return colorDim
	case "error":
		return colorRed
	default:
		return ""
	}
}

// shouldColorize returns true iff the writer is a *os.File pointing to a TTY
// AND NO_COLOR is unset. Anything else (a bytes.Buffer in tests, a redirected
// file, or a CI run with NO_COLOR=1) gets plain text.
func shouldColorize(w io.Writer) bool {
	if os.Getenv("NO_COLOR") != "" {
		return false
	}
	f, ok := w.(*os.File)
	if !ok {
		return false
	}
	fi, err := f.Stat()
	if err != nil {
		return false
	}
	return (fi.Mode() & os.ModeCharDevice) != 0
}
