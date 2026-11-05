"""Organization cleanup must fail closed and leave other tenants intact."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from hailhq.api.routes.internal.organization_deletion import (
    OrganizationDeletionIn,
    prepare_organization_deletion,
)
from hailhq.core.models import (
    Email,
    EmailAttachment,
    EmailAttachmentUpload,
    EmailDomain,
    Organization,
    OrganizationMember,
    OrgClosure,
    PhoneNumber,
)
from sqlalchemy.ext.asyncio import AsyncSession


async def seed(db, role="owner"):
    org, user = uuid4(), uuid4()
    db.add(Organization(id=org, name="Acme"))
    db.add(
        OrganizationMember(
            id=uuid4(),
            user_id=user,
            organization_id=org,
            role=role,
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return OrganizationDeletionIn(
        organization_id=org, user_id=user, confirmation_name="Acme"
    )


async def prepare(body, db, **overrides):
    mail = SimpleNamespace(delete=AsyncMock())
    provider = SimpleNamespace(delete_identity=AsyncMock())
    return await prepare_organization_deletion(
        body,
        db,
        overrides.get("email_provider", provider),
        None,
        lambda name, purpose: None,
        overrides.get("mail", mail),
    )


@pytest.mark.parametrize("role", ["admin", "member"])
async def test_only_owner_can_prepare(async_session: AsyncSession, role):
    body = await seed(async_session, role)
    with pytest.raises(HTTPException) as exc:
        await prepare(body, async_session)
    assert exc.value.status_code == 403
    assert await async_session.get(OrgClosure, body.organization_id) is None


async def test_name_must_match(async_session: AsyncSession):
    body = await seed(async_session)
    body.confirmation_name = "acme"
    with pytest.raises(HTTPException) as exc:
        await prepare(body, async_session)
    assert exc.value.status_code == 400
    assert await async_session.get(OrgClosure, body.organization_id) is None


async def test_pending_number_prevents_cleanup(async_session: AsyncSession):
    body = await seed(async_session)
    async_session.add(
        PhoneNumber(
            organization_id=body.organization_id,
            e164="+14155550123",
            country_code="US",
            number_type="local",
            provisioning_state="pending",
        )
    )
    await async_session.commit()
    with pytest.raises(HTTPException) as exc:
        await prepare(body, async_session)
    assert exc.value.status_code == 409
    assert await async_session.get(OrgClosure, body.organization_id) is None


async def test_erases_stored_files_before_acknowledging_and_preserves_shared_identity(
    async_session: AsyncSession,
):
    body = await seed(async_session)
    custom = EmailDomain(
        organization_id=body.organization_id, kind="custom", domain="example.com"
    )
    shared = EmailDomain(
        organization_id=body.organization_id,
        kind="hail_mail",
        domain="mail.hail.so",
        local_prefix_user="owner",
        local_prefix_org="acme",
    )
    async_session.add_all([custom, shared])
    await async_session.flush()
    email = Email(
        organization_id=body.organization_id,
        email_domain_id=custom.id,
        direction="outbound",
        from_address="a@example.com",
        to_addresses=["b@example.com"],
        subject="test",
        body_text="test",
        status="sent",
        raw_s3_key="mail/raw",
    )
    async_session.add(email)
    await async_session.flush()
    async_session.add(
        EmailAttachment(
            email_id=email.id,
            filename="test.txt",
            content_type="text/plain",
            size_bytes=1,
            s3_key="mail/attachment",
        )
    )
    async_session.add(
        EmailAttachmentUpload(
            organization_id=body.organization_id,
            filename="test.txt",
            content_type="text/plain",
            size_bytes=1,
            s3_key="mail/upload",
        )
    )
    await async_session.commit()
    mail = SimpleNamespace(delete=AsyncMock())
    provider = SimpleNamespace(delete_identity=AsyncMock())
    result = await prepare(body, async_session, mail=mail, email_provider=provider)
    assert result == {"organization_id": str(body.organization_id), "ready": True}
    assert {call.args[0] for call in mail.delete.await_args_list} == {
        "mail/raw",
        "mail/attachment",
        "mail/upload",
    }
    provider.delete_identity.assert_awaited_once_with("example.com")
    # Website owns the final transaction. References survive until it commits.
    assert await async_session.get(Email, email.id) is not None
    assert await async_session.get(Organization, body.organization_id) is not None
    assert await async_session.get(OrgClosure, body.organization_id) is not None


async def test_storage_failure_does_not_acknowledge_cleanup(
    async_session: AsyncSession,
):
    body = await seed(async_session)
    async_session.add(
        EmailAttachmentUpload(
            organization_id=body.organization_id,
            filename="x",
            content_type="text/plain",
            size_bytes=1,
            s3_key="mail/upload",
        )
    )
    await async_session.commit()
    mail = SimpleNamespace(delete=AsyncMock(side_effect=RuntimeError("storage failed")))
    with pytest.raises(RuntimeError, match="storage failed"):
        await prepare(body, async_session, mail=mail)
    assert await async_session.get(Organization, body.organization_id) is not None


async def test_shared_raw_mail_is_preserved(async_session: AsyncSession):
    body = await seed(async_session)
    other = uuid4()
    for org in (body.organization_id, other):
        async_session.add(
            Email(
                organization_id=org,
                direction="inbound",
                from_address="a@example.com",
                to_addresses=["b@example.com"],
                subject="Shared",
                body_text="test",
                status="received",
                raw_s3_key="mail/shared",
            )
        )
    await async_session.commit()
    mail = SimpleNamespace(delete=AsyncMock())
    await prepare(body, async_session, mail=mail)
    mail.delete.assert_not_awaited()


async def test_old_oauth_jwt_cannot_fall_through_to_another_org(
    async_session: AsyncSession, monkeypatch
):
    from hailhq.api import deps
    from hailhq.core.models import User

    body = await seed(async_session)
    cutoff = datetime.now(timezone.utc)
    async_session.add(
        User(
            id=body.user_id,
            name="Alex",
            email="alex@example.com",
            created_at=cutoff,
            oauth_tokens_valid_after=cutoff,
        )
    )
    await async_session.commit()
    monkeypatch.setattr(deps, "get_jwks_cache", lambda: object())
    monkeypatch.setattr(
        deps,
        "verify_jwt",
        AsyncMock(
            return_value={
                "sub": str(body.user_id),
                "azp": "existing-client",
                "iat": cutoff.timestamp() - 10,
            }
        ),
    )
    with pytest.raises(HTTPException) as exc:
        await deps._principal_from_jwt("old-signed-token", async_session)
    assert exc.value.status_code == 401


async def test_mutation_guard_drains_before_deletion_and_rejects_closed_org(
    session_factory,
):
    from hailhq.core.db import lock_open_organization
    from sqlalchemy import select, text
    from sqlalchemy.exc import DBAPIError

    async with session_factory() as setup_db:
        body = await seed(setup_db)
    async with session_factory() as writer, session_factory() as deleter:
        assert await lock_open_organization(writer, body.organization_id)
        await deleter.execute(text("SET LOCAL lock_timeout = '100ms'"))
        with pytest.raises(DBAPIError):
            await deleter.execute(
                select(Organization)
                .where(Organization.id == body.organization_id)
                .with_for_update()
            )
        await deleter.rollback()
        await writer.rollback()
        await deleter.execute(
            select(Organization)
            .where(Organization.id == body.organization_id)
            .with_for_update()
        )
        deleter.add(
            OrgClosure(
                organization_id=body.organization_id,
                closed_at=datetime.now(timezone.utc),
                source="organization_delete",
            )
        )
        await deleter.commit()
        assert not await lock_open_organization(writer, body.organization_id)


async def test_failed_order_retains_unreleased_resource_on_provider_failure(
    async_session: AsyncSession, monkeypatch
):
    from hailhq.api import number_orders

    body = await seed(async_session)
    row = PhoneNumber(
        organization_id=body.organization_id,
        e164="+14155550125",
        country_code="US",
        number_type="local",
        provisioning_state="failed",
        provisioning_metadata={"unreleased_resource_id": "paid-carrier-number"},
    )
    async_session.add(row)
    await async_session.commit()
    monkeypatch.setattr(
        number_orders,
        "carrier",
        lambda name: SimpleNamespace(
            release=AsyncMock(side_effect=RuntimeError("release failed"))
        ),
    )
    with pytest.raises(RuntimeError, match="release failed"):
        await prepare(body, async_session)
    assert row.provisioning_metadata["unreleased_resource_id"] == "paid-carrier-number"
    assert await async_session.get(Organization, body.organization_id) is not None


async def test_reused_identity_is_deleted_after_all_addresses(
    async_session: AsyncSession,
):
    from hailhq.core.models import CarrierVerification

    body = await seed(async_session)
    addresses = {"address-1", "address-2"}
    identity = {"exists": True}

    async def discard(refs, *, strict=False):
        addresses.discard(refs["address_id"])
        if refs.get("identity_created") and not addresses:
            identity["exists"] = False

    for index in (1, 2):
        async_session.add(
            CarrierVerification(
                organization_id=body.organization_id,
                provider="didww",
                country_code="GB",
                number_type="local",
                subject_type="business",
                subject_name=f"Business {index}",
                purpose="number",
                state="approved",
                requirements_version="test",
                provider_refs={
                    "address_id": f"address-{index}",
                    "identity_id": "shared-identity",
                    "identity_created": index == 1,
                },
            )
        )
    await async_session.commit()
    result = await prepare_organization_deletion(
        body,
        async_session,
        SimpleNamespace(delete_identity=AsyncMock()),
        None,
        lambda name, purpose: SimpleNamespace(discard=discard),
        SimpleNamespace(delete=AsyncMock()),
    )
    assert result["ready"] is True
    assert not identity["exists"]


async def test_failed_first_sms_attach_still_cleans_named_service(
    async_session: AsyncSession, monkeypatch
):
    from hailhq.api.routes.internal import organization_deletion

    body = await seed(async_session)
    async_session.add(
        PhoneNumber(
            organization_id=body.organization_id,
            e164="+14155550127",
            country_code="US",
            number_type="local",
            provisioning_state="failed",
            capabilities=["voice", "sms"],
            messaging_service_sid=None,
        )
    )
    await async_session.commit()
    state = {"service_exists": True}

    async def delete_services(org, ids):
        state["service_exists"] = False

    monkeypatch.setattr(
        organization_deletion,
        "sms_route",
        lambda name: SimpleNamespace(delete_organization_services=delete_services),
    )
    await prepare(body, async_session)
    assert not state["service_exists"]


async def test_http_guard_survives_route_commits(session_factory, monkeypatch):
    from hailhq.api import deps
    from sqlalchemy import select, text
    from sqlalchemy.exc import DBAPIError

    async with session_factory() as setup_db:
        body = await seed(setup_db)
    principal = deps.Principal(
        auth_kind="jwt",
        api_key_id=None,
        user_id=body.user_id,
        organization_id=body.organization_id,
        scopes=["*"],
    )
    monkeypatch.setattr(
        deps, "_authenticate_principal", AsyncMock(return_value=principal)
    )
    async with session_factory() as writer, session_factory() as deleter:
        request = deps.get_current_principal("Bearer test", writer)
        try:
            assert await anext(request) is principal
            await writer.commit()
            await deleter.execute(text("SET LOCAL lock_timeout = '100ms'"))
            with pytest.raises(DBAPIError):
                await deleter.execute(
                    select(Organization)
                    .where(Organization.id == body.organization_id)
                    .with_for_update()
                )
            await deleter.rollback()
        finally:
            await request.aclose()
        await deleter.execute(
            select(Organization)
            .where(Organization.id == body.organization_id)
            .with_for_update()
        )
