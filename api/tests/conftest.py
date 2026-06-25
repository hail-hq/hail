"""Test fixtures for the API service."""

from __future__ import annotations

import json as _jwt_json
import secrets
import uuid
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock

import httpx
import httpx as _httpx_for_jwt
import jwt as _jwt_lib
import pytest
from cryptography.hazmat.primitives import serialization as _serialization
from cryptography.hazmat.primitives.asymmetric import ed25519 as _ed25519
from jwt.algorithms import OKPAlgorithm as _OKPAlgorithm
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api import auth as _auth_module
from hailhq.api.auth import hash_key
from hailhq.api.main import app
from hailhq.api.routes.calls import get_livekit
from hailhq.api.routes.email_domains import get_email_provider
from hailhq.core.db import get_session
from hailhq.core.livekit import LiveKitClient
from hailhq.core.models import (
    AccountCredit,
    ApiKey,
    OrganizationMember,
    PhoneNumber,
)
from hailhq.core.providers.email import EmailProvider
from hailhq.core.providers.email.base import (
    DkimRecord,
    ProviderIdentity,
    ProviderSendResult,
)
from hailhq.core.testing.fixtures import (  # noqa: F401
    async_session,
    database_url,
    db,
    session_factory,
)


def mint_test_key() -> tuple[str, str]:
    """Generate an auth-backend-shaped plaintext + its storage hash."""
    plain = "hl_live_" + secrets.token_urlsafe(32)
    return plain, hash_key(plain)


async def insert_org_and_key(
    session: AsyncSession,
    *,
    org_name: str = "Acme",  # noqa: ARG001 — kept for backwards-compatible call sites
    org_slug: str = "acme",  # noqa: ARG001
    auth_user_id: str | None = None,
    initial_credit_cents: int = 100_000,
) -> tuple[uuid.UUID, ApiKey, str]:
    """Insert a member + apikey row tied to a synthetic organization id.

    Returns ``(organization_id, api_key, plaintext)``. ``organization_id`` is
    a freshly-generated UUID; the matching row in ``members`` is what
    ``deps.py`` joins on to resolve ``api_keys.reference_id`` →
    ``members.organization_id``.

    ``initial_credit_cents`` (default $1000) seeds a credit row so the
    balance gate on POST /v1/calls passes. Pass 0 for billing tests that
    specifically want a zero-balance org.
    """
    user_uuid = uuid.UUID(auth_user_id) if auth_user_id else uuid.uuid4()
    auth_user_id = str(user_uuid)
    organization_id = uuid.uuid4()

    now = datetime.now(timezone.utc)
    session.add(
        OrganizationMember(
            id=uuid.uuid4(),
            user_id=user_uuid,
            organization_id=organization_id,
            role="owner",
            created_at=now,
        )
    )

    if initial_credit_cents > 0:
        session.add(
            AccountCredit(
                organization_id=organization_id,
                kind="credit",
                channel="credit",
                amount_cents=initial_credit_cents,
                source="test.fixture",
                ref="test.fixture",
            )
        )

    plain, hashed = mint_test_key()
    api_key = ApiKey(
        id=uuid.uuid4(),
        name="test-key",
        start=plain[:14],
        reference_id=auth_user_id,
        prefix="hl_live_",
        key=hashed,
        created_at=now,
        updated_at=now,
    )
    session.add(api_key)
    await session.commit()
    await session.refresh(api_key)
    return organization_id, api_key, plain


@pytest.fixture()
def livekit_mock() -> AsyncMock:
    mock = AsyncMock(spec=LiveKitClient)
    mock.create_room.return_value = "hail-test-room"
    mock.dispatch_agent.return_value = "AD_test_dispatch"

    counter = {"n": 0}

    async def _make_participant(**kwargs):
        counter["n"] += 1
        return SimpleNamespace(
            sip_call_id=f"PA_test_sid_{counter['n']}",
            participant_identity=kwargs.get("participant_identity", "caller-test"),
            participant_id=f"PI_test_{counter['n']}",
            room_name=kwargs.get("room_name", "hail-test-room"),
        )

    mock.create_sip_participant.side_effect = _make_participant
    return mock


def _mail_from_dns_records(domain: str) -> list[DkimRecord]:
    """The MAIL FROM MX + SPF TXT records SES returns for a custom domain."""
    return [
        DkimRecord(
            name=f"send.{domain}",
            value="feedback-smtp.us-east-1.amazonses.com",
            type="MX",
            priority=10,
        ),
        DkimRecord(
            name=f"send.{domain}",
            value="v=spf1 include:amazonses.com ~all",
            type="TXT",
        ),
    ]


@pytest.fixture()
def email_mock() -> AsyncMock:
    """Default mock email provider — happy-path for every operation.

    Per-test reconfiguration is fine: assign to ``mock.send_email.side_effect``
    to simulate an SES error, or swap return values for create_identity.
    """
    mock = AsyncMock(spec=EmailProvider)

    counter = {"n": 0}

    async def _send(**kwargs):
        counter["n"] += 1
        return ProviderSendResult(provider_message_id=f"ses-msg-{counter['n']}")

    mock.send_email.side_effect = _send

    async def _create_identity(domain: str) -> ProviderIdentity:
        return ProviderIdentity(
            domain=domain,
            verification_status="pending",
            dkim_records=[
                DkimRecord(
                    name=f"t._domainkey.{domain}",
                    value="t.dkim.amazonses.com",
                ),
                *_mail_from_dns_records(domain),
            ],
            mail_from_domain=f"send.{domain}",
            mail_from_status="pending",
            provider_resource_id=domain,
        )

    mock.create_identity.side_effect = _create_identity

    async def _get_identity(domain: str) -> ProviderIdentity:
        # Default: caller has published DKIM, SES marks it verified.
        # Returns all 5 records (3 DKIM CNAMEs + MAIL FROM MX + SPF TXT) to
        # match real SesEmailProvider.get_identity behavior after the fix.
        return ProviderIdentity(
            domain=domain,
            verification_status="verified",
            dkim_records=[
                DkimRecord(
                    name=f"sel{i}._domainkey.{domain}",
                    value=f"sel{i}.dkim.amazonses.com",
                )
                for i in (1, 2, 3)
            ]
            + _mail_from_dns_records(domain),
            mail_from_domain=f"send.{domain}",
            mail_from_status="verified",
            provider_resource_id=domain,
        )

    mock.get_identity.side_effect = _get_identity
    mock.delete_identity.return_value = None
    return mock


@pytest.fixture()
async def client(
    async_session: AsyncSession,  # noqa: F811 (re-used as a fixture parameter name)
    livekit_mock: AsyncMock,
    email_mock: AsyncMock,
) -> AsyncIterator[httpx.AsyncClient]:
    async def override_get_session() -> AsyncIterator[AsyncSession]:
        yield async_session

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_livekit] = lambda: livekit_mock
    app.dependency_overrides[get_email_provider] = lambda: email_mock

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_livekit, None)
        app.dependency_overrides.pop(get_email_provider, None)


@pytest.fixture()
async def org_and_key(
    async_session: AsyncSession,  # noqa: F811 (re-used as a fixture parameter name)
) -> tuple[uuid.UUID, ApiKey, str]:
    return await insert_org_and_key(async_session)


@pytest.fixture(autouse=True)
def reset_deps_caches():
    """Clear deps.py process-wide caches between tests."""
    from hailhq.api import deps

    deps.reset_caches()
    _auth_module.reset_jwks_cache_for_testing()
    yield
    deps.reset_caches()
    _auth_module.reset_jwks_cache_for_testing()


@pytest.fixture()
def add_phone_number():
    """Factory fixture: ``await add_phone_number(session, org_id, e164=...)``.

    Pass ``organization_id=None`` together with ``is_pool=True`` to create a
    shared-pool number (the CHECK constraint requires both or neither).
    """

    async def _add(
        session: AsyncSession,
        organization_id: uuid.UUID | None,
        e164: str = "+14155551234",
        state: str = "active",
        provider_resource_id: str = "PN_test",
        is_pool: bool = False,
    ) -> PhoneNumber:
        pn = PhoneNumber(
            organization_id=organization_id,
            e164=e164,
            country_code="US",
            number_type="local",
            provider_resource_id=provider_resource_id,
            provisioning_state=state,
            is_pool=is_pool,
        )
        session.add(pn)
        await session.commit()
        await session.refresh(pn)
        return pn

    return _add


# ----------------------------------------------------------------------
# JWT fixtures — shared across api/tests/test_auth_jwt.py.
# ----------------------------------------------------------------------

_TEST_KID = "test-kid-1"


@pytest.fixture(scope="session")
def signing_keypair() -> tuple[bytes, dict[str, Any]]:
    """A throwaway Ed25519 keypair: (private_pem, public_jwk_dict)."""
    key = _ed25519.Ed25519PrivateKey.generate()
    private_pem = key.private_bytes(
        encoding=_serialization.Encoding.PEM,
        format=_serialization.PrivateFormat.PKCS8,
        encryption_algorithm=_serialization.NoEncryption(),
    )
    public_jwk = _jwt_json.loads(_OKPAlgorithm.to_jwk(key.public_key()))
    public_jwk["kid"] = _TEST_KID
    public_jwk["use"] = "sig"
    public_jwk["alg"] = "EdDSA"
    return private_pem, public_jwk


@pytest.fixture()
def jwks_dict(signing_keypair) -> dict[str, Any]:
    _, public_jwk = signing_keypair
    return {"keys": [public_jwk]}


@pytest.fixture()
def sign_jwt(signing_keypair) -> Callable[..., str]:
    """Returns a function that signs an EdDSA JWT with the test key."""
    private_pem, _ = signing_keypair

    def _sign(
        claims: dict[str, Any], *, kid: str = _TEST_KID, alg: str = "EdDSA"
    ) -> str:
        return _jwt_lib.encode(claims, private_pem, algorithm=alg, headers={"kid": kid})

    return _sign


@pytest.fixture()
def jwks_client_factory(jwks_dict) -> Callable[[], _httpx_for_jwt.AsyncClient]:
    """A factory returning httpx clients whose MockTransport serves the JWKS."""

    def _factory() -> _httpx_for_jwt.AsyncClient:
        def _handler(_req: _httpx_for_jwt.Request) -> _httpx_for_jwt.Response:
            return _httpx_for_jwt.Response(200, json=jwks_dict)

        return _httpx_for_jwt.AsyncClient(
            transport=_httpx_for_jwt.MockTransport(_handler)
        )

    return _factory


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


@pytest.fixture()
def base_claims() -> Callable[..., dict[str, Any]]:
    """Builds a minimal valid claim set, allowing per-test overrides."""

    def _make(
        *,
        sub: str | None = None,
        iss: str = "https://issuer.example.com",
        aud: str | Iterable[str] = "https://api.example.com",
        exp_offset_seconds: int = 300,
        scope: str | None = None,
        scopes: list[str] | None = None,
    ) -> dict[str, Any]:
        import uuid

        claims: dict[str, Any] = {
            "sub": sub or str(uuid.uuid4()),
            "iss": iss,
            "aud": list(aud) if not isinstance(aud, str) else aud,
            "exp": _now_ts() + exp_offset_seconds,
            "iat": _now_ts(),
        }
        if scope is not None:
            claims["scope"] = scope
        if scopes is not None:
            claims["scopes"] = scopes
        return claims

    return _make
