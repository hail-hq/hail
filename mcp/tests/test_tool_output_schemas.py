"""The public tool listing describes existing structured results."""

import pytest
from jsonschema import Draft202012Validator


async def test_all_tools_publish_concrete_output_schemas(tools_by_name):
    for name, tool in tools_by_name.items():
        schema = tool.output_schema
        assert schema is not None, name
        assert schema["type"] == "object", name
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        assert validator.is_valid({"error": "example failure"}), name
        assert not validator.is_valid({}), name


@pytest.mark.parametrize(
    ("tool", "result", "invalid"),
    [
        ("list_calls", {"items": [], "next_cursor": None}, {"items": "many"}),
        ("get_email_raw", {"url": "https://example.test/file"}, {"url": 42}),
        ("check_verification", {"ok": True, "files": [], "note": "Checked"}, {"ok": 1}),
        (
            "release_number",
            {"released": True, "number_id": "n", "e164": "+14155550100"},
            {"released": False},
        ),
        (
            "release_number",
            {"dismissed": True, "number_id": "n", "e164": "+14155550100"},
            {"dismissed": False},
        ),
    ],
)
async def test_output_schema_covers_real_result_variants(
    tools_by_name, tool, result, invalid
):
    validator = Draft202012Validator(tools_by_name[tool].output_schema)
    assert validator.is_valid(result)
    assert not validator.is_valid({**result, **invalid})
