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


def test_bounded_headers_keep_the_root_for_the_next_inbound_reply():
    original = Email(id=uuid4(), message_id="<original@example.test>")
    outgoing = Email(
        id=uuid4(),
        message_id="<outgoing@example.test>",
        references_ids=[
            original.message_id,
            *[f"<reply{n}@example.test>" for n in range(50)],
        ],
    )
    inbound = Email(
        id=uuid4(),
        message_id="<inbound@example.test>",
        references_ids=reference_ids(outgoing),
        in_reply_to=outgoing.message_id,
    )
    assert len(inbound.references_ids) == 30
    assert same_thread(original, inbound)


def test_long_message_ids_keep_root_and_recent_parent_within_service_budget():
    ids = [f"<{n:03d}-{'x' * 150}@example.test>" for n in range(30)]
    email = Email(id=uuid4(), references_ids=ids[:-1], in_reply_to=ids[-1])
    result = reference_ids(email)
    assert len(" ".join(result)) <= 995
    assert result[0] == ids[0] and result[-1] == ids[-1]
    assert len(reference_ids(email, limit=None)) == 30


def test_parent_wins_when_root_and_parent_cannot_fit_together():
    root = f"<{'x' * 500}@example.test>"
    parent = f"<{'y' * 500}@example.test>"
    email = Email(id=uuid4(), references_ids=[root], in_reply_to=parent)
    assert reference_ids(email) == [parent]
