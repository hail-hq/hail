import pytest
from validate_submission import ValidationError, validate

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
        validate(
            "# No frontmatter here\n\n## TODO\n## Steps to submit\n## Content\n## Notes\n"
        )


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
