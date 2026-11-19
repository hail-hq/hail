#!/usr/bin/env python3
"""Validate docs/submissions/<slug>.md files and the copy rules.

    validate_submission.py <submission.md>            full check of one submission file
    validate_submission.py --inbox-only <file> ...    only the "email inbox" rule, for any public text

Copy that must match the master text is wrapped in <!-- copy:NAME --> ... <!-- /copy -->.
The master text for each NAME is the block with the same markers in README.md (this folder).
"""

import re
import sys
from pathlib import Path

REQUIRED_KEYS = ["target", "slug", "category", "url", "score", "status"]
VALID_STATUSES = {"drafted", "submitted", "rejected", "n/a"}
REQUIRED_SECTIONS = ["## TODO", "## Steps to submit", "## Content", "## Notes"]
# "inbox" alone confuses non-native English readers: always write "email inbox".
INBOX = re.compile(r"(?<![/\w-])inbox(es)?\b", re.IGNORECASE)
EMAIL_BEFORE = re.compile(r"email\s+$", re.IGNORECASE)


def find_bare_inbox(text):
    """Return the 1-based line of the first "inbox" not preceded by "email"."""
    for match in INBOX.finditer(text):
        if not EMAIL_BEFORE.search(text[max(0, match.start() - 20) : match.start()]):
            return text[: match.start()].count("\n") + 1
    return None


COPY_BLOCK = re.compile(r"<!-- copy:([\w-]+) -->\n(.*?)\n\s*<!-- /copy -->", re.DOTALL)
MASTER_FILE = Path(__file__).resolve().parent / "README.md"


class ValidationError(Exception):
    pass


def normalize(block):
    """Drop blockquote marks and indentation, keep paragraph breaks."""
    lines = [re.sub(r"^\s*>?\s?", "", line).rstrip() for line in block.split("\n")]
    return "\n".join(lines).strip()


def copy_blocks(text):
    return [(m.group(1), normalize(m.group(2))) for m in COPY_BLOCK.finditer(text)]


def load_master(path=MASTER_FILE):
    return dict(copy_blocks(path.read_text()))


def check_copy_blocks(text, master):
    for name, block in copy_blocks(text):
        if name not in master:
            raise ValidationError(f"copy:{name} has no master block in README.md")
        if block != master[name]:
            raise ValidationError(
                f"copy:{name} differs from the master text in README.md"
            )


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
    return fields, text[match.end() :]


def validate(text, master=None):
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

    line = find_bare_inbox(text)
    if line:
        raise ValidationError(f'write "email inbox", not "inbox" alone (line {line})')

    if master is not None:
        check_copy_blocks(text, master)

    return fields


def main():
    args = sys.argv[1:]
    if args[:1] == ["--inbox-only"] and len(args) > 1:
        failed = 0
        for path in args[1:]:
            line = find_bare_inbox(Path(path).read_text())
            if line:
                print(
                    f'FAIL {path}:{line}: write "email inbox", not "inbox" alone',
                    file=sys.stderr,
                )
                failed = 1
        return failed
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    path = args[0]
    with open(path) as f:
        text = f.read()
    try:
        fields = validate(text, master=load_master())
    except ValidationError as e:
        print(f"FAIL {path}: {e}", file=sys.stderr)
        return 1
    print(
        f"OK {path}: {fields['target']} (score={fields['score']}, status={fields['status']})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
