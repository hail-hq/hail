"""FileUploadCreate: the body of POST /uploads."""

from __future__ import annotations

import pytest
from hailhq.core.schemas import FileUploadCreate
from pydantic import ValidationError


def _create(filename: str) -> FileUploadCreate:
    return FileUploadCreate(
        purpose="email_attachment", filename=filename, content_type="text/plain"
    )


@pytest.mark.parametrize(
    "filename",
    ["a\r\nBcc: x@example.com", "a\nb", "a\rb", "a\x00b", "a\tb", "a\x1fb", "a\x7fb"],
)
def test_filename_refuses_control_characters(filename: str) -> None:
    with pytest.raises(ValidationError) as exc:
        _create(filename)
    assert "control characters" in str(exc.value)


@pytest.mark.parametrize("filename", ["invoice.pdf", "Rapport mai 2026.pdf", "é.png"])
def test_filename_keeps_normal_names(filename: str) -> None:
    assert _create(filename).filename == filename
