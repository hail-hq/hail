"""Offline checks for the read-only audit's country/type matching."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "audit_regulatory", Path(__file__).with_name("audit-regulatory.py")
)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def test_empty_requirements_are_distinct_from_missing_regulations():
    rows = audit.build_matrix(
        [
            {"country_code": "US", "number_type": "local"},
            {"country_code": "ZZ", "number_type": "local"},
        ],
        [
            {
                "iso_country": "US",
                "number_type": "local",
                "end_user_type": "business",
                "requirements": {"end_user": [], "supporting_document": []},
            }
        ],
    )
    assert rows[0]["regulation_found"] is True
    assert rows[0]["bundle_required_for"] == []
    assert rows[1]["regulation_found"] is False


def test_toll_free_normalization_and_end_user_specific_requirements():
    rows = audit.build_matrix(
        [{"country_code": "GB", "number_type": "toll_free"}],
        [
            {
                "iso_country": "GB",
                "number_type": "toll-free",
                "end_user_type": "business",
                "requirements": {"end_user": [{"fields": ["business_name"]}]},
            },
            {
                "iso_country": "GB",
                "number_type": "toll-free",
                "end_user_type": "individual",
                "requirements": {"end_user": []},
            },
        ],
    )
    assert rows[0]["bundle_required_for"] == ["business"]
    assert len(rows[0]["requirements"]) == 1
