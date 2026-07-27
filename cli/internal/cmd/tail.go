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
	"sort"
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
// in lockstep so an unsupported `hail tail --id <type>:...` fails fast on
// the CLI without an HTTP round-trip. When a new channel lands on the API,
// add it here in the same change.
var supportedResourceTypes = []string{"call", "email", "sms"}

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

// splitResourceArg splits a CLI-supplied --id value into (type, idPart).
// A bare value with no "<type>:" prefix is treated as a call — `hail tail
// --id df10f471` mirrors `hail call tail df10f471`. Validation of the id
// part (full UUID vs 4+ char hex prefix) happens in resolveTailID.
func splitResourceArg(value string) (resType, idPart string, err error) {
	idx := strings.Index(value, ":")
	if idx < 0 {
		return "call", value, nil
	}
	resType = value[:idx]
	idPart = value[idx+1:]
	if resType == "" {
		return "", "", fmt.Errorf("missing resource type before ':'")
	}
	if idPart == "" {
		return "", "", fmt.Errorf("missing resource id after ':'")
	}
	for _, t := range supportedResourceTypes {
		if t == resType {
			return resType, idPart, nil
		}
	}
	return "", "", fmt.Errorf(
		"unsupported resource type %q; supported: %s",
		resType, strings.Join(supportedResourceTypes, ", "),
	)
}

// resolveTailID turns the id part into a full UUID. A full UUID passes
// through with no HTTP; anything else is treated as a hex prefix and
// resolved against the recent list for the resource type (calls and
// emails have list endpoints; sms rows don't yet).
func resolveTailID(ctx context.Context, apiClient *client.ClientWithResponses, resType, idPart string) (uuid.UUID, error) {
	if parsed, err := uuid.Parse(idPart); err == nil {
		return parsed, nil
	}
	switch resType {
	case "call":
		id, _, err := resolveCallID(ctx, apiClient, idPart)
		return id, err
	case "email":
		return resolveEmailID(ctx, apiClient, idPart)
	default:
		return uuid.Nil, fmt.Errorf(
			"invalid %s id %q: prefixes are not resolvable for %s, pass the full UUID",
			resType, idPart, resType,
		)
	}
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

// amdSentences maps AMD verdict categories (voicebot/hailhq/voicebot/amd.py)
// to human-readable [amd] lines. Unknown categories render verbatim so a
// new verdict doesn't need a CLI release to show up legibly.
var amdSentences = map[string]string{
	"human":               "answered by a human",
	"machine-ivr":         "answered by a machine (phone menu)",
	"machine-vm":          "answered by a machine (voicemail)",
	"machine-unavailable": "answered by a machine (mailbox unavailable)",
	"uncertain":           "unclear who answered — treating as human",
}

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

Without --id (or a positional), tail follows every event in the org,
prefixing each line with a short id ([c2a8f1d3]) so multiple in-flight
conversations disambiguate at a glance. Runs until Ctrl-C.

Narrow to one resource either way:

  hail tail --id call:<uuid>
  hail tail call:<uuid>
  hail tail df10f471            # bare id — treated as a call
  hail tail --id call:df10f471  # 4+ char prefix, resolved like git short hashes

Both forms accept '<type>:<id>' where <type> is one of: ` + strings.Join(supportedResourceTypes, ", ") + `,
and <id> is a full UUID or a 4+ char hex prefix (calls and emails). A bare
<id> with no type defaults to call.

When narrowed to a call, tail auto-exits when the call reaches a terminal
status (completed/failed/busy/no_answer/canceled). Email tails currently
run until SIGINT.`,
		Args: cobra.MaximumNArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if len(args) == 1 {
				if f.id != "" && args[0] != f.id {
					return helpAndFail(cmd, "--id and positional disagree")
				}
				f.id = args[0]
			}
			return runTail(cmd.Context(), opts, f)
		},
	}
	cmd.Flags().StringVar(&f.id, "id", "", "Narrow to one resource: '<type>:<uuid>', '<type>:<prefix>', or a bare call id/prefix; supported types: "+strings.Join(supportedResourceTypes, ", "))
	registerTailFlags(cmd, f)
	return cmd
}

// registerTailFlags binds the polling-shape flags (--kind, --interval,
// --from-start, --no-follow) onto a cobra command. Shared by `hail tail`,
// `hail call tail`, and `hail email tail` so the defaults and usage
// strings stay locked.
func registerTailFlags(cmd *cobra.Command, f *tailFlags) {
	cmd.Flags().StringVar(&f.kind, "kind", "", "Filter by event kind (server-side, exact match)")
	cmd.Flags().IntVar(&f.intervalMS, "interval", 500, "Poll interval in ms (100..10000)")
	cmd.Flags().BoolVar(&f.fromStart, "from-start", false, "Fetch all historical events first (default: start from now)")
	cmd.Flags().BoolVar(&f.noFollow, "no-follow", false, "Print one page and exit (no follow)")
}

func runTail(ctx context.Context, opts *Options, f *tailFlags) error {
	if f.intervalMS < 100 || f.intervalMS > 10000 {
		return fmt.Errorf("--interval must be in [100, 10000] ms, got %d", f.intervalMS)
	}

	// SIGINT cancels the poll loop. Exit 130 happens at Execute() — we just
	// return errInterrupted from here.
	tailCtx, stop := signal.NotifyContext(ctx, os.Interrupt, syscall.SIGTERM)
	defer stop()

	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}

	// Resolve --id: split fails fast on malformed / unsupported values with
	// no network IO, and a short prefix costs one list roundtrip (a full
	// UUID none) before polling starts.
	var (
		idWire       string // exact "<type>:<uuid>" string put on the wire
		resourceType string
	)
	if f.id != "" {
		rtype, idPart, err := splitResourceArg(f.id)
		if err != nil {
			return err
		}
		rid, err := resolveTailID(tailCtx, apiClient, rtype, idPart)
		if err != nil {
			return err
		}
		resourceType = rtype
		idWire = fmt.Sprintf("%s:%s", rtype, rid.String())
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
	// Generous limit so a single fetch can drain a long backlog.
	limit := 1000
	buildParams := func(cur string) *client.ListEventsEventsGetParams {
		return &client.ListEventsEventsGetParams{
			Limit:  &limit,
			Cursor: strPtr(cur),
			Id:     strPtr(idWire),
			Kind:   strPtr(f.kind),
		}
	}
	fetch := func(cur string) (*client.EventStreamResponse, error) {
		resp, err := apiClient.ListEventsEventsGetWithResponse(tailCtx, buildParams(cur))
		if err != nil {
			if tailCtx.Err() != nil {
				return nil, errInterrupted
			}
			return nil, fmt.Errorf("poll events: %w", err)
		}
		if idWire != "" && resp.HTTPResponse.StatusCode == http.StatusNotFound {
			return nil, fmt.Errorf("%s %s not found (or not in your org)", resourceType, f.id)
		}
		if resp.HTTPResponse.StatusCode != http.StatusOK || resp.JSON200 == nil {
			return nil, apiError(resp.HTTPResponse.StatusCode, resp.Body)
		}
		return resp.JSON200, nil
	}

	renderer := newTailRenderer(opts, resourceType != "", colorize, f.kind != "")
	// Buffered pickup transcripts must survive every exit path — fetch
	// errors, --no-follow, SIGINT. flushAll is idempotent, so the explicit
	// call before the terminal-status line below is safe to keep.
	defer renderer.flushAll()

	for {
		page, err := fetch(cursor)
		if err != nil {
			return err
		}
		firstPage := page

		// Drain inner pages — the server only sets next_cursor when more rows
		// exist beyond `limit`. In steady-state polling we drain everything in
		// one fetch; we synthesize the next polling cursor from the last seen
		// event so the next poll picks up after the events we just printed.
		var lastEvent *client.EventResponse
		for {
			for i := range page.Items {
				if err := renderer.renderEvent(page.Items[i]); err != nil {
					return err
				}
				lastEvent = &page.Items[i]
			}
			if page.NextCursor == nil || *page.NextCursor == "" {
				break
			}
			cursor = *page.NextCursor
			page, err = fetch(cursor)
			if err != nil {
				return err
			}
		}
		// Synthesize the forward cursor when the server didn't hand one back.
		if lastEvent != nil {
			cursor = encodeEventCursor(lastEvent.OccurredAt, lastEvent.Id)
		}

		if f.noFollow {
			return nil
		}

		// Auto-exit only when narrowed to a single call (--id call:<uuid>).
		// Org-wide tail and non-call resource types run until SIGINT. Read
		// the status off the first page to match the prior behavior — the
		// inner drain loop may span seconds.
		// Email auto-exit pending: EventStreamResponse does not yet expose an
		// EmailStatus field on the generated client. Email tails run until SIGINT.
		if resourceType == "call" &&
			firstPage.CallStatus != nil && terminalCallStatuses[*firstPage.CallStatus] {
			renderer.flushAll()
			finalLine := fmt.Sprintf("call %s", string(*firstPage.CallStatus))
			renderSystemLine(opts, time.Now().UTC(), finalLine, colorize)
			return nil
		}

		renderer.markQuiet()
		select {
		case <-tailCtx.Done():
			return errInterrupted
		case <-time.After(interval):
		}
	}
}

// tailRenderer owns cross-event display state: AMD-aware labeling of
// pickup transcripts, and silence dots between displayed lines. One
// instance lives for the whole tail loop.
//
// `singleResource` is true when --id <type>:<uuid> narrowed the stream; the
// short-id prefix is omitted in that mode (every event belongs to the same
// resource, the prefix would be redundant noise).
type tailRenderer struct {
	opts           *Options
	singleResource bool
	colorize       bool
	// kindFiltered is true when --kind narrows the stream server-side. A
	// partial stream disables both AMD buffering (the amd_result row may be
	// filtered out, so buffered turns would starve until call end) and
	// silence dots (gaps between filtered lines are not dead air).
	kindFiltered bool
	// pending buffers user_turn events per call until that call's AMD
	// verdict arrives, so a phone tree's menu prompt renders as [machine]
	// instead of [human]. The transcript events land in the stream before
	// the amd_result row, so the right label is unknowable on arrival.
	pending     map[openapi_types.UUID][]client.EventResponse
	amdResolved map[openapi_types.UUID]bool
	// machineActive marks calls whose AMD verdict was a machine and no
	// person has been detected yet — user_turn events in that window are
	// the phone tree talking, not a human.
	machineActive map[openapi_types.UUID]bool
	// lastShownAt is the event-timestamp of the last printed line; silence
	// dots derive from gaps between them. quietAnchor is its wall-clock
	// twin for live-mode waiting dots.
	lastShownAt time.Time
	quietAnchor time.Time
	hasShown    bool
}

func newTailRenderer(opts *Options, singleResource, colorize, kindFiltered bool) *tailRenderer {
	return &tailRenderer{
		opts:           opts,
		singleResource: singleResource,
		colorize:       colorize,
		kindFiltered:   kindFiltered,
		pending:        map[openapi_types.UUID][]client.EventResponse{},
		amdResolved:    map[openapi_types.UUID]bool{},
		machineActive:  map[openapi_types.UUID]bool{},
	}
}

// renderEvent dispatches on event.Kind and writes one line to opts.Stdout.
// In --json mode each event is emitted as a single JSON object per line,
// verbatim and unbuffered — the NDJSON stream must not be reordered.
func (r *tailRenderer) renderEvent(ev client.EventResponse) error {
	if r.opts.JSON {
		out, err := json.Marshal(ev)
		if err != nil {
			return fmt.Errorf("encode event JSON: %w", err)
		}
		fmt.Fprintln(r.opts.Stdout, string(out))
		return nil
	}

	res := eventResourceID(ev)
	if ev.Kind == "user_turn" && !r.kindFiltered && !r.amdResolved[res] {
		r.pending[res] = append(r.pending[res], ev)
		return nil
	}
	if ev.Kind == "amd_result" {
		r.amdResolved[res] = true
		category, _ := ev.Payload["category"].(string)
		machine := strings.HasPrefix(category, "machine")
		r.flushPending(res, machine)
		// The voicebot may keep transcribing the phone tree after the
		// verdict row (menu speech events land after amd_result) — keep
		// labeling that speech [machine] until a person is detected.
		r.machineActive[res] = machine
	} else if ev.Kind == "person_detected" {
		// The handoff marker means everything user-side before it was the
		// phone tree — a mid-join buffer (amd_result outside the window)
		// flushes as [machine], not [human].
		r.amdResolved[res] = true
		r.flushPending(res, true)
	} else if ev.Kind != "state_change" || len(r.pending[res]) > 0 {
		// The voicebot writes amd_result before any agent_turn, so any
		// post-pickup event kind means the verdict is not coming (AMD
		// skipped or failed, or the tail joined mid-call) — whoever speaks
		// from here is presumed a person, with no buffering delay.
		// state_change alone doesn't resolve: queued/ringing transitions
		// precede pickup in --from-start replays.
		r.amdResolved[res] = true
		r.flushPending(res, false)
	}
	// A machine phase ends when the voicebot reports the handoff
	// (person_detected) or, for events written before that kind existed,
	// when the agent first speaks to the person.
	if ev.Kind == "person_detected" || ev.Kind == "agent_turn" {
		r.machineActive[res] = false
	}
	if ev.Kind == "user_turn" && r.machineActive[res] {
		r.printLine(ev, "[machine]", colorDim, turnText(ev))
		return nil
	}
	label, body := renderEventBody(ev)
	r.printLine(ev, label, colorFor(ev.Kind), body)
	return nil
}

// flushPending prints a call's buffered pickup transcripts, labeled per
// the AMD verdict, preserving their original timestamps and order.
func (r *tailRenderer) flushPending(res openapi_types.UUID, machine bool) {
	for _, ev := range r.pending[res] {
		label, color := "[human]", colorYellow
		if machine {
			label, color = "[machine]", colorDim
		}
		r.printLine(ev, label, color, turnText(ev))
	}
	delete(r.pending, res)
}

// flushAll drains every buffer as [human] — the stream is ending, so no
// verdict is coming.
func (r *tailRenderer) flushAll() {
	for res := range r.pending {
		r.flushPending(res, false)
	}
}

// printLine writes one formatted line, preceded by silence dots when the
// event-timestamp gap since the previous line warrants them.
func (r *tailRenderer) printLine(ev client.EventResponse, label, color, body string) {
	r.renderSilence(ev.OccurredAt)
	ts := ev.OccurredAt.UTC().Format("15:04:05")
	if r.colorize && color != "" {
		label = color + label + colorReset
	}
	if r.singleResource {
		fmt.Fprintf(r.opts.Stdout, "[%s] %-9s %s\n", ts, label, body)
	} else {
		short := shortCallID(eventResourceID(ev))
		prefix := fmt.Sprintf("[%s]", short)
		if r.colorize {
			prefix = shortIDColor(short) + prefix + colorReset
		}
		fmt.Fprintf(r.opts.Stdout, "[%s] %s %-9s %s\n", ts, prefix, label, body)
	}
	r.lastShownAt = ev.OccurredAt
	r.quietAnchor = time.Now()
	r.hasShown = true
}

// renderSilence prints one dim "." line per second of gap between displayed
// lines, so dead air is visible instead of implied. Gaps beyond 10s
// compress to three dots plus a duration — a four-hour hold must not
// scroll fourteen thousand lines. Single-resource mode only: org-wide
// streams interleave calls, so gaps between lines mean nothing there.
func (r *tailRenderer) renderSilence(ts time.Time) {
	if !r.singleResource || r.kindFiltered || !r.hasShown {
		return
	}
	secs := int(ts.Sub(r.lastShownAt).Seconds())
	if secs < 2 {
		return
	}
	if secs > 10 {
		for i := 1; i <= 3; i++ {
			r.dotLine(r.lastShownAt.Add(time.Duration(i)*time.Second), ".")
		}
		r.dotLine(ts, fmt.Sprintf("(%s silent)", (time.Duration(secs)*time.Second).String()))
		return
	}
	for i := 1; i <= secs; i++ {
		r.dotLine(r.lastShownAt.Add(time.Duration(i)*time.Second), ".")
	}
}

// markQuiet prints live-mode waiting dots: one per full wall-clock second
// since the last printed line. It advances both anchors so the next
// event's timestamp gap doesn't re-print the same silence.
func (r *tailRenderer) markQuiet() {
	if !r.singleResource || r.kindFiltered || r.opts.JSON || !r.hasShown {
		return
	}
	now := time.Now()
	if secs := int(now.Sub(r.quietAnchor).Seconds()); secs > 10 {
		// Wall-clock jump — machine suspend, a blocked terminal — not a
		// live second-by-second wait. Compress like renderSilence instead
		// of flooding one dot line per elapsed second.
		for i := 1; i <= 3; i++ {
			r.dotLine(r.lastShownAt.Add(time.Duration(i)*time.Second), ".")
		}
		advance := time.Duration(secs) * time.Second
		r.dotLine(r.lastShownAt.Add(advance), fmt.Sprintf("(%s silent)", advance.String()))
		r.quietAnchor = r.quietAnchor.Add(advance)
		r.lastShownAt = r.lastShownAt.Add(advance)
		return
	}
	for now.Sub(r.quietAnchor) >= time.Second {
		r.dotLine(r.lastShownAt.Add(time.Second), ".")
		r.quietAnchor = r.quietAnchor.Add(time.Second)
		r.lastShownAt = r.lastShownAt.Add(time.Second)
	}
}

// dotLine writes one waiting line — "[13:50:57] [wait]    ." — timestamped
// like every other line so silence reads in sequence with the transcript.
func (r *tailRenderer) dotLine(ts time.Time, s string) {
	line := fmt.Sprintf("[%s] %-9s %s", ts.UTC().Format("15:04:05"), "[wait]", s)
	if r.colorize {
		line = colorDim + line + colorReset
	}
	fmt.Fprintln(r.opts.Stdout, line)
}

// eventResourceID picks the id (call, email, or sms) that owns this event,
// for the per-resource short-id prefix in org-wide tail.
// EventResponse.CallId, .EmailId, and .SmsId are all optional now that the
// stream is unified across sources; the server sets exactly one. Falls back
// to uuid.Nil if somehow none is set.
func eventResourceID(ev client.EventResponse) openapi_types.UUID {
	if ev.CallId != nil {
		return *ev.CallId
	}
	if ev.EmailId != nil {
		return *ev.EmailId
	}
	if ev.SmsId != nil {
		return *ev.SmsId
	}
	return openapi_types.UUID(uuid.Nil)
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
func renderEventBody(ev client.EventResponse) (label, body string) {
	switch ev.Kind {
	case "state_change":
		from, _ := ev.Payload["from"].(string)
		to, _ := ev.Payload["to"].(string)
		if from == "" && to == "" {
			return "[system]", payloadJSON(ev.Payload)
		}
		return "[system]", fmt.Sprintf("%s → %s", from, to)
	case "agent_turn":
		return "[hail]", turnText(ev)
	case "user_turn":
		return "[human]", turnText(ev)
	case "amd_result":
		// The pickup transcript lines above already show what was heard —
		// render only the verdict, as a sentence, not the payload JSON.
		if category, ok := ev.Payload["category"].(string); ok && category != "" {
			if sentence, ok := amdSentences[category]; ok {
				return "[amd]", sentence
			}
			return "[amd]", category
		}
		return "[amd]", payloadJSON(ev.Payload)
	case "person_detected":
		// Written by the voicebot when a person comes on the line after a
		// machine answered (post-IVR handoff).
		return "[system]", "a person is on the line"
	case "tool_call":
		// Current shape (voicebot/.../agent.py): {"tools": [names],
		// "calls": [{"name", "args"}]} — `calls` carries arguments so the
		// line shows what the tool did, e.g. send_dtmf(digits=2). Older
		// rows have only `tools`; spec mentions {"name", "args"} too —
		// handle all three defensively.
		if calls, ok := ev.Payload["calls"].([]interface{}); ok && len(calls) > 0 {
			if rendered := renderToolCalls(calls); rendered != "" {
				return "[tool]", rendered
			}
		}
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

// renderToolCalls formats the tool_call `calls` payload as
// "name(k=v, ...)" entries joined by "; ". Returns "" on a malformed
// list so the caller can fall through to the older shapes.
func renderToolCalls(calls []interface{}) string {
	parts := make([]string, 0, len(calls))
	for _, c := range calls {
		m, ok := c.(map[string]interface{})
		if !ok {
			continue
		}
		name, _ := m["name"].(string)
		if name == "" {
			continue
		}
		args, _ := m["args"].(map[string]interface{})
		kvs := make([]string, 0, len(args))
		for k, v := range args {
			kvs = append(kvs, fmt.Sprintf("%s=%v", k, v))
		}
		sort.Strings(kvs)
		parts = append(parts, fmt.Sprintf("%s(%s)", name, strings.Join(kvs, ", ")))
	}
	return strings.Join(parts, "; ")
}

// turnText is the spoken text of a turn event, falling back to the raw
// payload JSON when the text field is absent or empty.
func turnText(ev client.EventResponse) string {
	text, _ := ev.Payload["text"].(string)
	if text == "" {
		return payloadJSON(ev.Payload)
	}
	return text
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
	case "state_change", "amd_result", "person_detected":
		return colorDim
	case "error":
		return colorRed
	default:
		return ""
	}
}

// isTTY reports whether the writer is a *os.File pointing at a terminal.
// Anything else (a bytes.Buffer in tests, a redirected file, a pipe) is
// not a TTY. Shared by color logic and by the binary-fetch commands that
// refuse to dump bytes onto an interactive terminal.
func isTTY(w io.Writer) bool {
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

// shouldColorize returns true iff the writer is a TTY and NO_COLOR is unset.
func shouldColorize(w io.Writer) bool {
	if os.Getenv("NO_COLOR") != "" {
		return false
	}
	return isTTY(w)
}
