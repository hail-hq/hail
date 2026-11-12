from uuid import uuid4

from hailhq.core.email_threads import reference_ids, same_thread
from hailhq.core.models import Email


def test_long_thread_keeps_access_to_original_files_but_bounds_reply_headers():
    original = Email(id=uuid4(), message_id="<original@example.test>")
    newest = Email(
        id=uuid4(),
        message_id="<latest@example.test>",
        references_ids=[
            original.message_id,
            *[f"<reply{n}@example.test>" for n in range(50)],
        ],
    )
    assert same_thread(original, newest)
    assert len(reference_ids(newest)) == 30


def test_reply_headers_do_not_accept_injected_header_lines():
    email = Email(
        id=uuid4(),
        message_id="<safe@example.test>",
        references_ids=["<bad>\r\nBcc: stranger@example.test"],
    )
    assert reference_ids(email) == ["<safe@example.test>"]
