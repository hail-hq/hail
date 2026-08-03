# Hail Registry & Directory Submissions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the two-phase Workflow (research → user approval → draft) that produces submission-ready files for the highest-impact registries, directories, and subreddits for Hail and Hail MCP, per `docs/superpowers/specs/2026-07-06-registry-submissions-design.md`.

**Architecture:** Two named `.claude/workflows/*.js` Workflow scripts (research, drafting) plus a small file-format convention (frontmatter + fixed sections) enforced by a standalone validator script, so every generated `docs/submissions/<slug>.md` is mechanically checkable. Workflow scripts cannot touch the filesystem themselves — each run returns structured data that a plan task then writes to disk with the `Write` tool.

**Tech Stack:** `Workflow` tool (JS-subset orchestration scripts), Python 3 stdlib + pytest (validator), Node 24 (`--check` for script syntax gating).

## Global Constraints

- **No git commits.** Every task ends with changes left uncommitted for the user to review and commit themselves — no `git add`/`git commit` steps anywhere in this plan.
- **No autonomous execution of external actions.** Every registry/subreddit gets a drafted file; nothing gets posted, filed, or opened as a PR automatically.
- **Feature-claim policy:** core-capability claims (voice, SMS, email, analytics, deliverability) are written as shipped/present-tense regardless of milestone checkbox state. Only provider/vendor breadth (which specific carrier/vendor backs a capability) stays accurate to what's actually wired up in `core/hailhq/core/providers/`; unshipped breadth goes on a "coming soon" note, never implied as live.
- **Brand voice/visual identity source:** Claude Design project "Hail.so Visual Identity" (`e175bb75-8f2d-48c6-9106-05354e94fdc1`) — tone is direct/operational/wry/technical (`identity.html` §05 Voice), logo/wordmark rules from `logos/README.md`. `launch/show-hn.html` in that same project is tone/pacing reference only — never a fact source (it describes a stale local-install flow and the wrong GitHub org `hail-so` instead of the actual `hail-hq`).
- **Non-feature facts verified against code, not prose:** wired-up providers in `core/hailhq/core/providers/<channel>/`, live MCP tool list in `mcp/hailhq/mcp/tools.py`, what's actually published in `pyproject.toml`/`cli/`/npm scope `@hail-hq/`, in-flight work in `CHANGELOG.md` and `docs/superpowers/specs/*`. Correct org path is `github.com/hail-hq/hail`.
- **Output convention:** every submission file has YAML frontmatter (`target`, `slug`, `category`, `url`, `score`, `status`) and four sections (`## TODO`, `## Steps to submit`, `## Content`, `## Notes`); the index (`docs/submissions/README.md`) is sorted by score, highest first.
- **Handoff safety:** both Workflow scripts must be resumable (`resumeFromRunId`), state must live in files not memory, and every generated step must be imperative/literal — no step that requires inference or "figure it out" judgment calls by whoever executes it next.

---

## File Structure

```
docs/submissions/
  validate_submission.py       # structural validator (Task 1)
  test_validate_submission.py  # tests for the validator (Task 1)
  _TEMPLATE.md                 # reference template, validates clean (Task 2)
  README.md                    # index skeleton -> populated index (Task 2, then Task 6)
  <slug>.md                    # one per approved target (generated in Task 6)
.claude/workflows/
  hail-registry-research.js    # Phase 1 Workflow script (Task 3)
  hail-registry-drafting.js    # Phase 2 Workflow script (Task 5)
```

---

### Task 1: Submission-file validator

**Files:**

- Create: `docs/submissions/validate_submission.py`
- Create: `docs/submissions/test_validate_submission.py`

**Interfaces:**

- Consumes: nothing (no dependency on other tasks — uses inline fixtures)
- Produces: `validate(text: str) -> dict[str, str]` (returns parsed frontmatter fields, raises `ValidationError` on any structural problem), `ValidationError` exception class, and a CLI entry point `python3 validate_submission.py <path>` returning exit code 0/1/2. Task 2 and Task 6 both call this CLI.

- [ ] **Step 1: Write the failing tests**

Create `docs/submissions/test_validate_submission.py`:

```python
import pytest
from validate_submission import validate, ValidationError

GOOD = """---
target: "Official MCP Registry"
slug: mcp-registry
category: mcp-registry
url: https://github.com/modelcontextprotocol/registry
score: 92
status: drafted
---

# Official MCP Registry

## TODO
- [ ] Verify domain ownership via DNS TXT record

## Steps to submit
1. Run `mcp-publisher login github`
2. Run `mcp-publisher publish`

## Content
Hail gives your agents a phone number, inbox, and SMS line.

## Notes
Requires DNS access to hail.so.
"""


def test_valid_submission_passes():
    fields = validate(GOOD)
    assert fields["slug"] == "mcp-registry"
    assert fields["status"] == "drafted"


def test_missing_frontmatter_fails():
    with pytest.raises(ValidationError, match="frontmatter"):
        validate("# No frontmatter here\n\n## TODO\n## Steps to submit\n## Content\n## Notes\n")


def test_missing_required_key_fails():
    bad = GOOD.replace('target: "Official MCP Registry"\n', "")
    with pytest.raises(ValidationError, match="missing keys"):
        validate(bad)


def test_bad_status_fails():
    bad = GOOD.replace("status: drafted", "status: maybe")
    with pytest.raises(ValidationError, match="status"):
        validate(bad)


def test_non_numeric_score_fails():
    bad = GOOD.replace("score: 92", "score: high")
    with pytest.raises(ValidationError, match="score"):
        validate(bad)


def test_missing_section_fails():
    bad = GOOD.replace("## Notes\nRequires DNS access to hail.so.\n", "")
    with pytest.raises(ValidationError, match="missing sections"):
        validate(bad)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/r/playground/hail && uv run pytest docs/submissions/test_validate_submission.py -v`
Expected: `ModuleNotFoundError: No module named 'validate_submission'` (or collection error) — the module doesn't exist yet.

- [ ] **Step 3: Write the implementation**

Create `docs/submissions/validate_submission.py`:

```python
#!/usr/bin/env python3
"""Validate a docs/submissions/<slug>.md file has the required frontmatter and sections."""
import re
import sys

REQUIRED_KEYS = ["target", "slug", "category", "url", "score", "status"]
VALID_STATUSES = {"drafted", "submitted", "rejected", "n/a"}
REQUIRED_SECTIONS = ["## TODO", "## Steps to submit", "## Content", "## Notes"]


class ValidationError(Exception):
    pass


def parse_frontmatter(text):
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        raise ValidationError("missing frontmatter block delimited by '---'")
    fields = {}
    for line in match.group(1).splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            raise ValidationError(f"malformed frontmatter line: {line!r}")
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip().strip('"')
    return fields, text[match.end():]


def validate(text):
    fields, body = parse_frontmatter(text)

    missing = [k for k in REQUIRED_KEYS if k not in fields]
    if missing:
        raise ValidationError(f"frontmatter missing keys: {missing}")

    if fields["status"] not in VALID_STATUSES:
        raise ValidationError(
            f"status {fields['status']!r} not one of {sorted(VALID_STATUSES)}"
        )

    try:
        float(fields["score"])
    except ValueError:
        raise ValidationError(f"score {fields['score']!r} is not numeric")

    missing_sections = [s for s in REQUIRED_SECTIONS if s not in body]
    if missing_sections:
        raise ValidationError(f"missing sections: {missing_sections}")

    return fields


def main():
    if len(sys.argv) != 2:
        print("usage: validate_submission.py <path/to/submission.md>", file=sys.stderr)
        return 2
    path = sys.argv[1]
    with open(path) as f:
        text = f.read()
    try:
        fields = validate(text)
    except ValidationError as e:
        print(f"FAIL {path}: {e}", file=sys.stderr)
        return 1
    print(f"OK {path}: {fields['target']} (score={fields['score']}, status={fields['status']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/r/playground/hail && uv run pytest docs/submissions/test_validate_submission.py -v`
Expected: all 6 tests PASS.

- [ ] **Step 5: Leave uncommitted**

Do not run `git add` / `git commit` — per Global Constraints, leave the new files uncommitted for the user to review.

---

### Task 2: Submission template and README index skeleton

**Files:**

- Create: `docs/submissions/_TEMPLATE.md`
- Create: `docs/submissions/README.md`

**Interfaces:**

- Consumes: `validate_submission.py` CLI from Task 1 (used to verify the template)
- Produces: the exact frontmatter/section shape that Task 3/5's Workflow prompts must reproduce, and the exact README table header that Task 6's generated index must match: `| Rank | Target | Category | Score | Status | File |`

- [ ] **Step 1: Write the template**

Create `docs/submissions/_TEMPLATE.md`:

```markdown
---
target: "Example Target Name"
slug: example-target
category: mcp-registry
url: https://example.com/submit
score: 0
status: drafted
---

# Example Target Name

## TODO

- [ ] Confirm eligibility / account requirements
- [ ] Draft copy reviewed against the feature-claim policy
- [ ] Assets attached (logo/screenshot references)
- [ ] Submitted
- [ ] Confirmed live

## Steps to submit

1. Go to <submission URL>.
2. Create an account if required.
3. Paste the fields from **Content** below into the form.
4. Attach assets from `hail-website/public/assets/` as specified.
5. Submit and record the resulting listing URL in **Notes**.

## Content

<Copy-paste-ready description, tags, install snippet, etc. — the exact fields this target's form asks for.>

## Notes

<Anything submission-specific: review turnaround time, contact email used, roadmap items not yet shipped.>
```

- [ ] **Step 2: Verify the template validates**

Run: `cd /Users/r/playground/hail && uv run python3 docs/submissions/validate_submission.py docs/submissions/_TEMPLATE.md`
Expected: `OK docs/submissions/_TEMPLATE.md: Example Target Name (score=0, status=drafted)`

- [ ] **Step 3: Write the README index skeleton**

Create `docs/submissions/README.md`:

```markdown
# Submission tracker

Ranked by relevance/impact score, highest first. Update the **Status** column as work progresses: `drafted` → `submitted` → `submitted (live)` or `rejected` / `n/a`.

| Rank | Target | Category | Score | Status | File |
| ---- | ------ | -------- | ----- | ------ | ---- |
```

- [ ] **Step 4: Verify the README skeleton**

Run: `grep -c '| Rank | Target | Category | Score | Status | File |' /Users/r/playground/hail/docs/submissions/README.md`
Expected: `1`

- [ ] **Step 5: Leave uncommitted**

Do not run `git add` / `git commit`.

---

### Task 3: Phase 1 — Research Workflow script

**Files:**

- Create: `.claude/workflows/hail-registry-research.js`

**Interfaces:**

- Consumes: nothing at authoring time (real inputs are fetched live by its agents)
- Produces: when run via the `Workflow` tool, returns `{ ranked: [{ name, url, category, submission_mechanism, requirements, score, rationale }], cut_line_index, cut_line_rationale }` — Task 4 consumes this return value directly (as `args.approved`, trimmed, for Task 5/6).

- [ ] **Step 1: Write the workflow script**

Create `.claude/workflows/hail-registry-research.js`:

```js
export const meta = {
  name: "hail-registry-research",
  description:
    "Find and score registries, directories, and subreddits for Hail + Hail MCP submissions",
  phases: [{ title: "Research" }, { title: "Synthesize" }],
};

const CANDIDATE_SCHEMA = {
  type: "object",
  properties: {
    candidates: {
      type: "array",
      items: {
        type: "object",
        properties: {
          name: { type: "string" },
          url: { type: "string" },
          submission_mechanism: { type: "string" },
          requirements: { type: "string" },
          eligibility_fit: { type: "string" },
          reach_estimate: { type: "string" },
          effort_estimate: { type: "string" },
          risk_notes: { type: "string" },
        },
        required: [
          "name",
          "url",
          "submission_mechanism",
          "requirements",
          "eligibility_fit",
          "reach_estimate",
          "effort_estimate",
        ],
      },
    },
  },
  required: ["candidates"],
};

const SHORTLIST_SCHEMA = {
  type: "object",
  properties: {
    ranked: {
      type: "array",
      items: {
        type: "object",
        properties: {
          name: { type: "string" },
          url: { type: "string" },
          category: { type: "string" },
          submission_mechanism: { type: "string" },
          requirements: { type: "string" },
          score: { type: "number" },
          rationale: { type: "string" },
        },
        required: [
          "name",
          "url",
          "category",
          "submission_mechanism",
          "requirements",
          "score",
          "rationale",
        ],
      },
    },
    cut_line_index: { type: "number" },
    cut_line_rationale: { type: "string" },
  },
  required: ["ranked", "cut_line_index", "cut_line_rationale"],
};

const CATEGORIES = [
  {
    key: "mcp-registry",
    prompt:
      "Hail is a self-hostable, open-source (AGPLv3) universal communication platform for AI agents (voice calls, SMS, and email). It exposes a remote MCP server (Streamable HTTP, no stdio, no local install) at a URL an agent client connects to directly. Research the submission process for these MCP-specific registries/directories: the official MCP registry (github.com/modelcontextprotocol/registry), mcp.so, Smithery, Glama, PulseMCP, OpenTools, the Cursor MCP directory, and the Claude.ai connector directory. For each, fetch their actual current submission docs and report name, url, submission_mechanism, requirements, eligibility_fit, reach_estimate, effort_estimate, risk_notes via the structured schema.",
  },
  {
    key: "github-list",
    prompt:
      'Research submission processes for curated GitHub "awesome list" repos relevant to Hail: awesome-mcp-servers and awesome-selfhosted (Hail is self-hostable and AGPLv3-licensed). Fetch each list\'s actual CONTRIBUTING guidelines and report name, url, submission_mechanism, requirements, eligibility_fit, reach_estimate, effort_estimate, risk_notes via the structured schema.',
  },
  {
    key: "ai-directory",
    prompt:
      "Research submission processes for general AI-tool directories with meaningful developer traffic: There's An AI For That, Futurepedia, Toolify, and any other directory in this space worth including. Fetch each site's actual submission page and report name, url, submission_mechanism, requirements, eligibility_fit, reach_estimate, effort_estimate, risk_notes via the structured schema.",
  },
  {
    key: "dev-directory",
    prompt:
      "Research submission processes for developer/startup launch directories: Product Hunt, Hacker News (Show HN), AlternativeTo. Fetch each site's actual submission guidelines and report name, url, submission_mechanism, requirements, eligibility_fit, reach_estimate, effort_estimate, risk_notes via the structured schema.",
  },
  {
    key: "subreddit",
    prompt:
      'Find subreddits relevant to AI agents, dev tools, self-hosting, voice AI, and MCP (consider r/LocalLLaMA, r/ClaudeAI, r/mcp, r/selfhosted, r/SideProject, r/opensource, and any others that fit). For each, read the actual sidebar/wiki rules to confirm self-promotion is allowed or there is a standing "showcase what you built" thread. In requirements describe the specific rule/thread that permits a post, and in submission_mechanism describe the compliant post angle (a showcase of a concrete use case, never an announcement). Use risk_notes for removal/ban risk. Report via the structured schema.',
  },
];

const found = await parallel(
  CATEGORIES.map(
    (c) => () =>
      agent(c.prompt, {
        label: `research:${c.key}`,
        phase: "Research",
        schema: CANDIDATE_SCHEMA,
      }).then((r) =>
        r ? r.candidates.map((cand) => ({ ...cand, category: c.key })) : [],
      ),
  ),
);

const allCandidates = found.filter(Boolean).flat();
log(
  `${allCandidates.length} candidates found across ${CATEGORIES.length} categories`,
);

const shortlist = await agent(
  `Score and rank these submission candidates for Hail (self-hostable AI-agent communication platform: voice, SMS, email; AGPLv3; remote MCP server) on reach x ICP-fit x effort. Weight subreddit candidates down for ban/removal risk. Sort "ranked" descending by score. Set cut_line_index to the 0-based index after which additional entries stop being worth the drafting effort, and explain why in cut_line_rationale. Candidates:\n\n${JSON.stringify(allCandidates, null, 2)}`,
  {
    label: "synthesize-shortlist",
    phase: "Synthesize",
    schema: SHORTLIST_SCHEMA,
  },
);

return shortlist;
```

- [ ] **Step 2: Verify the script is syntactically valid**

Run: `node --check /Users/r/playground/hail/.claude/workflows/hail-registry-research.js`
Expected: no output, exit code 0.

- [ ] **Step 3: Leave uncommitted**

Do not run `git add` / `git commit`.

---

### Task 4: Run the Research Workflow and get user approval

**Files:** none created — this is an execution + checkpoint task.

**Interfaces:**

- Consumes: `.claude/workflows/hail-registry-research.js` (Task 3)
- Produces: `approvedList` — a JS array subset of the workflow's `ranked` output, in the exact shape Task 5/6 expect: `{ name, url, category, submission_mechanism, requirements, score, rationale }[]`

- [ ] **Step 1: Invoke the workflow**

Call the `Workflow` tool with `{ scriptPath: ".claude/workflows/hail-registry-research.js" }` (no `args` needed — the script has no external inputs). This runs in the background; wait for the completion notification.

- [ ] **Step 2: Verify the returned shape**

Read the workflow's return value (from the tool result, or from `<transcriptDir>/journal.jsonl` if resuming). Confirm it has `ranked` (non-empty array), `cut_line_index` (number within `[0, ranked.length)`), and `cut_line_rationale` (non-empty string). If any candidate object is missing a required field, the run failed schema validation — do not proceed; re-run instead of hand-patching the output.

- [ ] **Step 3: Present the ranked list and get approval**

Show the user the full `ranked` list (already sorted by score descending) with the `cut_line_index` called out, plus `cut_line_rationale`. Ask which entries to keep. Record the user's final approved subset as `approvedList` — this is the exact array that Task 6 will pass as `args.approved` to the drafting workflow. **Do not proceed to Task 5/6 execution until the user has explicitly approved a list.**

- [ ] **Step 4: Leave uncommitted**

No files change in this task; nothing to commit regardless.

---

### Task 5: Phase 2 — Drafting Workflow script

**Files:**

- Create: `.claude/workflows/hail-registry-drafting.js`

**Interfaces:**

- Consumes: `args.approved` — array shaped exactly like Task 4's `approvedList` (`{ name, url, category, submission_mechanism, requirements, score, rationale }[]`)
- Produces: `{ files: [{ slug, filepath, content, name, category, score }], readme: string }` — Task 6 consumes this directly (writes `files[i].filepath` with `files[i].content`, and writes `docs/submissions/README.md` with `readme`)

- [ ] **Step 1: Write the workflow script**

Create `.claude/workflows/hail-registry-drafting.js`:

```js
export const meta = {
  name: "hail-registry-drafting",
  description:
    "Draft submission-ready files for the approved registry/directory/subreddit shortlist",
  phases: [{ title: "Draft" }, { title: "Fact-check" }],
};

function slugify(name) {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/(^-|-$)/g, "");
}

function draftPrompt(item) {
  const base = `Draft a submission file for "${item.name}" (${item.url}), category ${item.category}, submission mechanism: ${item.submission_mechanism}. Requirements: ${item.requirements}.

Hail is a self-hostable, AGPLv3, universal communication platform for AI agents — voice calls, SMS, and email, consumed via CLI, Python SDK, OpenAPI, and a remote MCP server (Streamable HTTP, no stdio/local install). Brand voice: direct, operational, wry, technical — say "Phone, SMS & email — for agents," never "revolutionary AI-powered omnichannel platform." Write every core-capability claim (voice, SMS, email, analytics, deliverability) as shipped and present-tense. Only provider/vendor breadth (which specific carrier/vendor backs a capability) must stay accurate to what's actually wired up in core/hailhq/core/providers/ — anything not wired up goes under a "coming soon" note in Content or Notes, never implied as already live.

Produce the FULL content of a markdown file with this exact structure and nothing else:

---
target: "${item.name}"
slug: ${slugify(item.name)}
category: ${item.category}
url: "${item.url}"
score: ${item.score}
status: drafted
---

# ${item.name}

## TODO
(checklist of concrete prerequisites for THIS target: account needed?, assets ready?, draft reviewed?, submitted?, confirmed live?)

## Steps to submit
(numbered, concrete, literal steps — where to go, what to paste, what to click — a non-expert must be able to follow them without guessing)

## Content
(the actual copy-paste-ready fields this target's submission form/post asks for: one-liner, description, tags, install/usage snippet, asset paths from hail-website/public/assets/)

## Notes
(anything submission-specific: review turnaround, contact used, roadmap items not yet shipped)`;

  if (
    item.category === "mcp-registry" &&
    /official|modelcontextprotocol\/registry/i.test(item.url)
  ) {
    return (
      base +
      `

This is the OFFICIAL MCP registry. The Content section must include the actual server.json manifest content for Hail's remote MCP server, and the Steps section must include the DNS/GitHub ownership-verification steps and the exact mcp-publisher CLI commands the user runs themselves (we do not hold their DNS/GitHub credentials).`
    );
  }
  if (item.category === "subreddit") {
    return (
      base +
      `

This is a subreddit. The Content section must be a "here's what I built" showcase post (a concrete use case demo), never a listing/announcement, and must explicitly comply with the rule/thread noted in requirements.`
    );
  }
  return base;
}

function factcheckPrompt(draftMarkdown, item) {
  return `Fact-check this drafted submission file against the ACTUAL current code, not just README prose:
- Wired-up providers: core/hailhq/core/providers/<channel>/
- Live MCP tool list: mcp/hailhq/mcp/tools.py
- What's actually published externally: pyproject.toml (SDK), cli/ release process, npm scope @hail-hq/
- In-flight work: CHANGELOG.md and docs/superpowers/specs/*

Policy: leave every core-capability claim (voice, SMS, email, analytics, deliverability) exactly as shipped/present-tense — do NOT walk those back. ONLY correct: (a) provider/vendor-breadth overclaims — e.g. implying Telnyx, Whisper, or AssemblyAI are wired up when only Twilio/Deepgram/Cartesia are; move any such claim to a "coming soon" note instead, (b) non-feature facts that are wrong: URLs, install/setup steps, license, tool names, org/repo paths (the correct org is github.com/hail-hq/hail, never hail-so). Return the corrected full markdown file content, nothing else, preserving the exact frontmatter and section structure.

Draft:
${draftMarkdown}`;
}

const results = await pipeline(
  args.approved,
  (item) =>
    agent(draftPrompt(item), { label: `draft:${item.name}`, phase: "Draft" }),
  (draftMarkdown, item) =>
    agent(factcheckPrompt(draftMarkdown, item), {
      label: `factcheck:${item.name}`,
      phase: "Fact-check",
    }).then((corrected) => ({
      slug: slugify(item.name),
      filepath: `docs/submissions/${slugify(item.name)}.md`,
      content: corrected,
      name: item.name,
      category: item.category,
      score: item.score,
    })),
);

const files = results.filter(Boolean).sort((a, b) => b.score - a.score);

const readmeRows = files
  .map(
    (f, i) =>
      `| ${i + 1} | ${f.name} | ${f.category} | ${f.score} | drafted | [${f.slug}.md](./${f.slug}.md) |`,
  )
  .join("\n");

const readme = `# Submission tracker

Ranked by relevance/impact score, highest first. Update the **Status** column as work progresses: \`drafted\` → \`submitted\` → \`submitted (live)\` or \`rejected\` / \`n/a\`.

| Rank | Target | Category | Score | Status | File |
|------|--------|----------|-------|--------|------|
${readmeRows}
`;

return { files, readme };
```

- [ ] **Step 2: Verify the script is syntactically valid**

Run: `node --check /Users/r/playground/hail/.claude/workflows/hail-registry-drafting.js`
Expected: no output, exit code 0.

- [ ] **Step 3: Leave uncommitted**

Do not run `git add` / `git commit`.

---

### Task 6: Run the Drafting Workflow, write files, verify

**Files:**

- Create: `docs/submissions/<slug>.md` (one per approved target — exact filenames depend on Task 4's approved list, not enumerable in advance)
- Modify: `docs/submissions/README.md` (replace skeleton from Task 2 with the populated index)

**Interfaces:**

- Consumes: `.claude/workflows/hail-registry-drafting.js` (Task 5), `approvedList` (Task 4), `validate_submission.py` CLI (Task 1)
- Produces: final deliverable — the committed-to-disk (but not git-committed) submission files

- [ ] **Step 1: Invoke the drafting workflow**

Call the `Workflow` tool with `{ scriptPath: ".claude/workflows/hail-registry-drafting.js", args: { approved: approvedList } }` using the `approvedList` recorded in Task 4 Step 3. Wait for completion.

- [ ] **Step 2: Write each returned file**

For every entry in the workflow's returned `files` array, use the `Write` tool to create `files[i].filepath` (relative to `/Users/r/playground/hail/`) with content `files[i].content`.

- [ ] **Step 3: Write the populated README index**

Use the `Write` tool to overwrite `docs/submissions/README.md` with the workflow's returned `readme` string.

- [ ] **Step 4: Validate every generated submission file**

Run:

```bash
cd /Users/r/playground/hail
for f in docs/submissions/*.md; do
  base="$(basename "$f")"
  [ "$base" = "README.md" ] && continue
  uv run python3 docs/submissions/validate_submission.py "$f"
done
```

Expected: one `OK ...` line per file (including `_TEMPLATE.md`), no `FAIL` lines. If any file fails, fix that specific file's frontmatter/sections directly (don't touch the workflow scripts for a one-off content bug) and re-run.

- [ ] **Step 5: Leave uncommitted**

Do not run `git add` / `git commit` — the user reviews and commits `docs/submissions/` themselves when ready.
