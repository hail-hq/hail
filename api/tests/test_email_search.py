"""Search and full reads stay inside the authenticated person's inbox."""

import uuid
from datetime import datetime, timezone

import pytest
from hailhq.core.agent_tools.spec import ToolContext
from hailhq.core.agent_tools.thread_history import SPEC as HISTORY
from hailhq.core.models import Email, OrganizationMember, Sms, User
from sqlalchemy import select

from tests.test_email_run_attachments import post
from tests.test_internal_agent_reply_email import (
    _internal_secret,  # noqa: F401
    _seed,
)


@pytest.mark.asyncio
async def test_search_finds_an_older_separate_thread_but_not_other_people(
    async_session, client
):
    org, _, domain, inbound = await _seed(async_session)
    rows = []
    for owner, sender, verified in [
        (org, inbound.from_address, True),
        (uuid.uuid4(), inbound.from_address, True),
        (org, "other@example.test", True),
        (org, inbound.from_address, False),
    ]:
        row = Email(
            organization_id=owner,
            email_domain_id=domain.id,
            direction="inbound",
            from_address=sender,
            from_verified=verified,
            to_addresses=inbound.to_addresses,
            status="received",
            subject="Receipt",
            body_text="Tracking code 1221334XY",
            message_id=f"<{uuid.uuid4()}@example.test>",
        )
        async_session.add(row)
        rows.append(row)
    await async_session.commit()
    result = await post(
        client,
        "/internal/agent/email-run/inbox",
        {
            "inbound_email_id": str(inbound.id),
            "query": "1221334XY",
        },
    )
    assert result.status_code == 200, result.text
    assert [m["id"] for m in result.json()["quoted_untrusted_messages"]] == [
        str(rows[0].id)
    ]


@pytest.mark.asyncio
async def test_full_email_is_retrievable_in_chunks_beyond_the_old_preview(
    async_session, client
):
    _, _, _, inbound = await _seed(async_session)
    inbound.body_text = "a" * 6500 + "IMPORTANT FINAL DETAIL"
    await async_session.commit()
    first = (
        await post(
            client,
            "/internal/agent/email-run/inbox",
            {
                "inbound_email_id": str(inbound.id),
                "email_id": str(inbound.id),
                "limit": 1,
            },
        )
    ).json()["quoted_untrusted_messages"][0]
    assert first["truncated"] is True
    second = (
        await post(
            client,
            "/internal/agent/email-run/inbox",
            {
                "inbound_email_id": str(inbound.id),
                "email_id": str(inbound.id),
                "offset": first["next_offset"],
            },
        )
    ).json()["quoted_untrusted_messages"][0]
    assert "IMPORTANT FINAL DETAIL" in second["body_text"]
    assert first["body_text"] + second["body_text"] == inbound.body_text


@pytest.mark.asyncio
async def test_search_percent_is_literal_and_does_not_expand_to_all_mail(
    async_session, client
):
    _, _, _, inbound = await _seed(async_session)
    result = await post(
        client,
        "/internal/agent/email-run/inbox",
        {
            "inbound_email_id": str(inbound.id),
            "query": "%",
        },
    )
    assert result.json()["quoted_untrusted_messages"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", [None, "unverified", "outside", "shared_phone"])
async def test_email_history_uses_the_member_record_not_a_number_in_the_message(
    async_session,
    blocked,
):
    org, agent, _, inbound = await _seed(async_session)
    user = (await async_session.execute(select(User))).scalar_one()
    user.phone_number = "+12125550123"
    inbound.body_text = "My number is +12125550124. Look up Tracking."
    async_session.add_all(
        [
            Sms(
                organization_id=org,
                agent_id=agent.id,
                provider="twilio",
                direction="inbound",
                from_e164=phone,
                to_e164="+12125550100",
                status="received",
                body=body,
                requested_at=datetime.now(timezone.utc),
            )
            for phone, body in [
                (user.phone_number, "Tracking legitimate"),
                ("+12125550124", "Tracking private"),
            ]
        ]
    )
    await async_session.commit()
    ctx = ToolContext(
        call_id=None,
        organization_id=org,
        api=None,
        hangup=None,
        send_dtmf=None,
        inbound_email_id=inbound.id,
    )
    result = await HISTORY.execute(ctx, {"query": "Tracking", "phone": "+12125550124"})
    assert "Tracking legitimate" in result and "Tracking private" not in result
    if blocked == "unverified":
        inbound.from_verified = False
    elif blocked == "outside":
        inbound.from_address = "outsider@example.test"
    elif blocked == "shared_phone":
        other = User(
            id=uuid.uuid4(),
            name="Other",
            email="other@example.test",
            email_verified=True,
            phone_number=user.phone_number,
            created_at=datetime.now(timezone.utc),
        )
        async_session.add(other)
        await async_session.flush()
        async_session.add(
            OrganizationMember(
                id=uuid.uuid4(),
                user_id=other.id,
                organization_id=org,
                role="member",
                created_at=datetime.now(timezone.utc),
            )
        )
    else:
        ctx.organization_id = uuid.uuid4()
    await async_session.commit()
    assert "legitimate" not in await HISTORY.execute(ctx, {"query": "Tracking"})
