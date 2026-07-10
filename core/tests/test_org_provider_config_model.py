"""OrgProviderConfig model: shape, defaults, and the multi-config-one-active rule."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from hailhq.core.models import OrgProviderConfig


async def test_round_trip_defaults(async_session) -> None:
    org_id = uuid.uuid4()
    row = OrgProviderConfig(
        organization_id=org_id,
        layer="tts",
        provider="cartesia",
        params={"voice_id": "694f9389-aac1-45b6"},
    )
    async_session.add(row)
    await async_session.commit()

    got = (
        await async_session.execute(
            select(OrgProviderConfig).where(OrgProviderConfig.organization_id == org_id)
        )
    ).scalar_one()
    assert got.layer == "tts"
    assert got.provider == "cartesia"
    assert got.encrypted_api_key is None
    assert got.key_last4 is None
    assert got.key_set_at is None
    assert got.params == {"voice_id": "694f9389-aac1-45b6"}
    assert got.fallback_enabled is False
    assert got.is_active is False
    assert got.created_at is not None


async def test_multiple_providers_per_org_and_layer_allowed(async_session) -> None:
    """Distinct providers for the same (org, layer) can now coexist."""
    org_id = uuid.uuid4()
    async_session.add(
        OrgProviderConfig(organization_id=org_id, layer="llm", provider="anthropic")
    )
    await async_session.commit()
    async_session.add(
        OrgProviderConfig(organization_id=org_id, layer="llm", provider="google")
    )
    await async_session.commit()

    rows = (
        (
            await async_session.execute(
                select(OrgProviderConfig).where(
                    OrgProviderConfig.organization_id == org_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert {row.provider for row in rows} == {"anthropic", "google"}


async def test_duplicate_org_layer_provider_rejected(async_session) -> None:
    org_id = uuid.uuid4()
    async_session.add(
        OrgProviderConfig(organization_id=org_id, layer="llm", provider="anthropic")
    )
    await async_session.commit()
    async_session.add(
        OrgProviderConfig(organization_id=org_id, layer="llm", provider="anthropic")
    )
    with pytest.raises(IntegrityError):
        await async_session.commit()


async def test_only_one_active_config_per_org_and_layer(async_session) -> None:
    org_id = uuid.uuid4()
    async_session.add(
        OrgProviderConfig(
            organization_id=org_id, layer="llm", provider="anthropic", is_active=True
        )
    )
    await async_session.commit()
    async_session.add(
        OrgProviderConfig(
            organization_id=org_id, layer="llm", provider="google", is_active=True
        )
    )
    with pytest.raises(IntegrityError):
        await async_session.commit()


async def test_layer_check_constraint(async_session) -> None:
    async_session.add(
        OrgProviderConfig(organization_id=uuid.uuid4(), layer="email", provider="ses")
    )
    with pytest.raises(IntegrityError):
        await async_session.commit()
