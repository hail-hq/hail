"""Tool input rules match the API's.

Most value sets come from ``hailhq.core`` types the API also uses. The rest
(page sizes, inline enums, the verification purposes, the confirm code
length) are restated in ``tools.py``; these tests compare them with
``openapi/openapi.yaml`` and the API's own constants, so a change on one
side fails CI until the other side follows.
"""

from __future__ import annotations

from pathlib import Path
from typing import get_args

import pytest
import yaml
from hailhq.mcp.tools import VerificationPurpose

_OPENAPI = yaml.safe_load(
    (Path(__file__).resolve().parents[2] / "openapi" / "openapi.yaml").read_text()
)

# (tool, argument) -> (API path, query parameter)
_QUERY_PARAMS = {
    ("list_calls", "limit"): ("/v1/calls", "limit"),
    ("list_calls", "status"): ("/v1/calls", "status"),
    ("list_sms", "limit"): ("/v1/sms", "limit"),
    ("list_sms", "status"): ("/v1/sms", "status"),
    ("list_emails", "limit"): ("/v1/emails", "limit"),
    ("list_emails", "status"): ("/v1/emails", "status"),
    ("list_emails", "direction"): ("/v1/emails", "direction"),
    ("get_email_events", "limit"): ("/v1/emails/{email_id}/events", "limit"),
    ("get_email_stats", "bucket"): ("/v1/emails/stats", "bucket"),
    ("get_events", "limit"): ("/v1/events", "limit"),
    ("list_numbers", "limit"): ("/v1/numbers", "limit"),
    ("list_email_domains", "limit"): ("/v1/email-domains", "limit"),
    ("list_contacts", "limit"): ("/v1/contacts", "limit"),
}


def _rules(schema: dict) -> dict:
    """The validation keywords of a property, with a nullable wrapper removed."""
    branches = [b for b in schema.get("anyOf", [schema]) if b.get("type") != "null"]
    (branch,) = branches
    return {k: branch[k] for k in ("enum", "minimum", "maximum") if k in branch}


def _api_param(path: str, name: str) -> dict:
    params = _OPENAPI["paths"][path]["get"]["parameters"]
    return next(p["schema"] for p in params if p["name"] == name)


@pytest.mark.parametrize("tool,arg", list(_QUERY_PARAMS))
async def test_tool_argument_matches_api_query(tools_by_name, tool, arg) -> None:
    mine = _rules(tools_by_name[tool].input_schema["properties"][arg])
    theirs = _rules(_api_param(*_QUERY_PARAMS[tool, arg]))
    assert mine and mine == theirs


def test_verification_purposes_match_api() -> None:
    from hailhq.api.routes.verifications import PURPOSES

    assert set(get_args(VerificationPurpose)) == PURPOSES


async def test_confirm_code_length_matches_api(tools_by_name) -> None:
    from hailhq.api.routes.verifications import ConfirmRequest

    code = tools_by_name["confirm_verification"].input_schema["properties"]["code"]
    api = ConfirmRequest.model_json_schema()["properties"]["code"]
    assert (code["minLength"], code["maxLength"]) == (
        api["minLength"],
        api["maxLength"],
    )
