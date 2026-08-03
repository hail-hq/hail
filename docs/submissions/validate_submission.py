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
    return fields, text[match.end() :]


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
    print(
        f"OK {path}: {fields['target']} (score={fields['score']}, status={fields['status']})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
