"""Connection service: create, update, rotate, delete."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from hailhq.api import connected_numbers
from hailhq.api.connected_numbers import (
    CHECK_LINE_SECONDS,
    CHECK_MAX_DURATION_SECONDS,
    CHECK_RESULT_WAIT_SECONDS,
    CheckResult,
    ConnectedNumberError,
    add_connected_number,
    check_connected_number,
    create_connection,
    delete_connection,
    remove_connected_number,
    remove_stale_unverified_numbers,
    rotate_inbound_password,
    update_connection,
)
from hailhq.core.billing import CALL_META_BILLED
from hailhq.core.config import settings
from hailhq.core.db import session_scope
from hailhq.core.inbound_calls import (
    CheckArrived,
    SipAttributes,
    finish_check,
    open_inbound_call,
)
from hailhq.core.livekit import LiveKitClient
from hailhq.core.models import (
    AccountCredit,
    Call,
    CallEvent,
    PhoneNumber,
    SipConnection,
    UsageEvent,
    WebhookDelivery,
    WebhookSubscription,
)
from hailhq.core.pool import CALL_META_FROM_POOL
from hailhq.core.secret_cipher import SecretKeyMissing, generate_key
from hailhq.core.sip_connections import (
    CHECK_CALL_META,
    CHECK_CODE_META,
    CHECK_CODE_SECONDS,
    CHECK_EVENT_KIND,
    CHECK_RING_SECONDS,
    CHECK_WINDOW,
    PBX_CHECK_LINE_SECONDS,
    check_in_flight,
    decrypt_secret,
    hash_check_code,
)
from livekit.api import SipCallError
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

ORG = UUID("00000000-0000-0000-0000-0000000000a6")


@pytest.fixture(autouse=True)
def secret_key(monkeypatch):
    monkeypatch.setattr(settings, "hail_provider_secret_key", generate_key())


@pytest.fixture()
def lk():
    client = AsyncMock(spec=LiveKitClient)
    client.create_pbx_inbound_trunk.return_value = "ST_in"
    client.create_pbx_outbound_trunk.return_value = "ST_out"
    return client


@pytest.fixture()
async def conn(async_session, lk):
    row, password = await create_connection(
        async_session,
        lk,
        organization_id=ORG,
        label="Main office",
        default_region="US",
        pbx_host="pbx.example.com",
    )
    row.test_password = password
    lk.reset_mock()
    return row


@pytest.fixture()
async def answer_only_conn(async_session, lk):
    row, _ = await create_connection(
        async_session,
        lk,
        organization_id=ORG,
        label="Main office",
        default_region="GB",
    )
    lk.reset_mock()
    return row


async def _number(session, conn, state, e164="+14155550100"):
    number = PhoneNumber(
        organization_id=ORG,
        e164=e164,
        country_code="US",
        number_type="local",
        capabilities=["voice"],
        provider="pbx",
        sip_connection_id=conn.id,
        provisioning_state=state,
    )
    session.add(number)
    await session.commit()
    return number


async def test_create_connection_makes_both_trunks_and_stores_secrets(
    async_session, lk
):
    conn, password = await create_connection(
        async_session,
        lk,
        organization_id=ORG,
        label="Main office",
        default_region="US",
        pbx_host="pbx.example.com",
    )
    assert len(password) >= 24
    assert conn.livekit_inbound_trunk_id == "ST_in"
    assert conn.livekit_outbound_trunk_id == "ST_out"
    assert decrypt_secret(conn.encrypted_inbound_password) == password
    lk.create_pbx_inbound_trunk.assert_awaited_once_with(
        conn.id, conn.inbound_username, password
    )


async def test_create_connection_stores_pbx_password_encrypted(async_session, lk):
    conn, _ = await create_connection(
        async_session,
        lk,
        organization_id=ORG,
        label=" Main office ",
        default_region="US",
        pbx_host="pbx.example.com",
        pbx_username="hail",
        pbx_password="s3cret",
    )
    assert conn.label == "Main office"
    assert decrypt_secret(conn.encrypted_pbx_password) == "s3cret"
    lk.create_pbx_outbound_trunk.assert_awaited_once_with(
        conn.id, "pbx.example.com", 5060, "auto", "hail", "s3cret"
    )


async def test_create_connection_answer_only_has_no_outbound_trunk(async_session, lk):
    conn, _ = await create_connection(
        async_session,
        lk,
        organization_id=ORG,
        label="Main office",
        default_region="GB",
    )
    assert conn.livekit_outbound_trunk_id is None
    lk.create_pbx_outbound_trunk.assert_not_awaited()


@pytest.mark.parametrize(
    "field,kwargs",
    [
        ("label", {"label": " "}),
        ("label", {"label": "x" * 81}),
        ("default_region", {"default_region": "XX"}),
        ("pbx_host", {"pbx_host": "https://pbx.example.com"}),
        ("pbx_host", {"pbx_host": "pbx.example.com:5060"}),
        ("pbx_host", {"pbx_host": "pbx.example.com/x"}),
        ("pbx_host", {"pbx_host": "a" * 254}),
        ("pbx_transport", {"pbx_transport": "ws"}),
        ("pbx_port", {"pbx_port": 0}),
        ("pbx_port", {"pbx_port": 65536}),
        ("pbx_password", {"pbx_password": "only-password"}),
    ],
)
async def test_create_connection_rejects_bad_input(async_session, lk, field, kwargs):
    args = {
        "organization_id": ORG,
        "label": "Main office",
        "default_region": "US",
        **kwargs,
    }
    with pytest.raises(ConnectedNumberError) as e:
        await create_connection(async_session, lk, **args)
    assert (e.value.code, e.value.field) == ("invalid_input", field)
    lk.create_pbx_inbound_trunk.assert_not_awaited()


async def test_create_connection_without_key_fails_before_livekit(
    async_session, lk, monkeypatch
):
    monkeypatch.setattr(settings, "hail_provider_secret_key", "")
    with pytest.raises(SecretKeyMissing):
        await create_connection(
            async_session,
            lk,
            organization_id=ORG,
            label="Main office",
            default_region="US",
        )
    lk.create_pbx_inbound_trunk.assert_not_awaited()


async def test_create_connection_deletes_trunks_when_insert_fails(
    async_session, lk, monkeypatch
):
    monkeypatch.setattr(
        async_session, "flush", AsyncMock(side_effect=IntegrityError("x", {}, None))
    )
    with pytest.raises(IntegrityError):
        await create_connection(
            async_session,
            lk,
            organization_id=ORG,
            label="Main office",
            default_region="US",
            pbx_host="pbx.example.com",
        )
    assert {c.args[0] for c in lk.delete_sip_trunk.await_args_list} == {
        "ST_in",
        "ST_out",
    }


async def test_create_connection_deletes_inbound_when_outbound_fails(async_session, lk):
    lk.create_pbx_outbound_trunk.side_effect = RuntimeError("boom")
    with pytest.raises(ConnectedNumberError) as e:
        await create_connection(
            async_session,
            lk,
            organization_id=ORG,
            label="Main office",
            default_region="US",
            pbx_host="pbx.example.com",
        )
    assert e.value.code == "livekit_failed"
    lk.delete_sip_trunk.assert_awaited_once_with("ST_in")


async def test_update_connection_adds_and_drops_outbound_trunk(
    async_session, lk, answer_only_conn
):
    conn = await update_connection(
        async_session, lk, answer_only_conn, pbx_host="pbx.example.com"
    )
    assert conn.livekit_outbound_trunk_id == "ST_out"
    conn = await update_connection(async_session, lk, conn, pbx_host=None)
    assert conn.livekit_outbound_trunk_id is None
    lk.delete_sip_trunk.assert_awaited_once_with("ST_out")


async def test_update_connection_host_to_host_updates_trunk(async_session, lk, conn):
    await update_connection(async_session, lk, conn, pbx_host="pbx2.example.com")
    assert lk.update_pbx_outbound_trunk.await_args.args[:2] == (
        "ST_out",
        "pbx2.example.com",
    )
    lk.create_pbx_outbound_trunk.assert_not_awaited()


async def test_update_connection_label_only_leaves_trunks_alone(
    async_session, lk, conn
):
    await update_connection(async_session, lk, conn, label="Branch")
    assert conn.label == "Branch"
    lk.update_pbx_outbound_trunk.assert_not_awaited()
    lk.create_pbx_outbound_trunk.assert_not_awaited()
    lk.delete_sip_trunk.assert_not_awaited()


async def test_update_connection_new_password_reaches_trunk(async_session, lk, conn):
    await update_connection(
        async_session, lk, conn, pbx_username="hail", pbx_password="pw2"
    )
    assert lk.update_pbx_outbound_trunk.await_args.args == (
        "ST_out",
        "pbx.example.com",
        5060,
        "auto",
        "hail",
        "pw2",
    )
    assert decrypt_secret(conn.encrypted_pbx_password) == "pw2"


async def test_update_connection_rejects_bad_input_before_livekit(
    async_session, lk, conn
):
    with pytest.raises(ConnectedNumberError) as e:
        await update_connection(async_session, lk, conn, pbx_port=70000)
    assert (e.value.code, e.value.field) == ("invalid_input", "pbx_port")
    lk.update_pbx_outbound_trunk.assert_not_awaited()


async def test_update_connection_refuses_deleted(async_session, lk, conn):
    await delete_connection(async_session, lk, conn)
    with pytest.raises(ConnectedNumberError) as e:
        await update_connection(async_session, lk, conn, label="x")
    assert e.value.code == "connection_deleted"


async def test_update_connection_reads_the_row_under_the_lock(async_session, lk, conn):
    """A connection deleted by another request after this one loaded it is
    refused before any LiveKit call."""
    async with session_scope() as other:
        await other.execute(
            update(SipConnection)
            .where(SipConnection.id == conn.id)
            .values(deleted_at=NOW)
        )
        await other.commit()
    with pytest.raises(ConnectedNumberError) as e:
        await update_connection(async_session, lk, conn, pbx_host="pbx2.example.com")
    assert e.value.code == "connection_deleted"
    lk.update_pbx_outbound_trunk.assert_not_awaited()
    lk.create_pbx_outbound_trunk.assert_not_awaited()


async def test_update_connection_restores_the_trunk_when_commit_fails(
    async_session, lk, conn_with_password, monkeypatch
):
    monkeypatch.setattr(
        async_session, "commit", AsyncMock(side_effect=IntegrityError("x", {}, None))
    )
    with pytest.raises(IntegrityError):
        await update_connection(
            async_session,
            lk,
            conn_with_password,
            pbx_host="pbx2.example.com",
            pbx_port=5080,
            pbx_password="new-pw",
        )
    calls = lk.update_pbx_outbound_trunk.await_args_list
    assert len(calls) == 2
    assert calls[0].args == (
        "ST_out",
        "pbx2.example.com",
        5080,
        "auto",
        "hail",
        "new-pw",
    )
    assert calls[1].args == (
        "ST_out",
        "pbx.example.com",
        5060,
        "auto",
        "hail",
        "old-pw",
    )
    lk.delete_sip_trunk.assert_not_awaited()


async def test_update_connection_deletes_a_new_trunk_when_commit_fails(
    async_session, lk, answer_only_conn, monkeypatch
):
    monkeypatch.setattr(
        async_session, "commit", AsyncMock(side_effect=IntegrityError("x", {}, None))
    )
    with pytest.raises(IntegrityError):
        await update_connection(
            async_session, lk, answer_only_conn, pbx_host="pbx.example.com"
        )
    lk.delete_sip_trunk.assert_awaited_once_with("ST_out")


async def test_update_connection_restore_failure_still_raises_the_commit_error(
    async_session, lk, conn, monkeypatch
):
    monkeypatch.setattr(
        async_session, "commit", AsyncMock(side_effect=IntegrityError("x", {}, None))
    )
    lk.update_pbx_outbound_trunk.side_effect = [None, RuntimeError("down")]
    with pytest.raises(IntegrityError):
        await update_connection(async_session, lk, conn, pbx_host="pbx2.example.com")
    assert lk.update_pbx_outbound_trunk.await_count == 2


async def test_rotate_inbound_password_updates_trunk_and_row(async_session, lk, conn):
    old = decrypt_secret(conn.encrypted_inbound_password)
    new = await rotate_inbound_password(async_session, lk, conn)
    lk.set_pbx_inbound_credentials.assert_awaited_once_with(
        "ST_in", conn.inbound_username, new
    )
    assert decrypt_secret(conn.encrypted_inbound_password) == new
    assert new != old


async def test_rotate_inbound_password_keeps_old_when_livekit_fails(
    async_session, lk, conn
):
    old = conn.encrypted_inbound_password
    lk.set_pbx_inbound_credentials.side_effect = RuntimeError("boom")
    with pytest.raises(ConnectedNumberError) as e:
        await rotate_inbound_password(async_session, lk, conn)
    assert e.value.code == "livekit_failed"
    await async_session.refresh(conn)  # the failure rolled back
    assert conn.encrypted_inbound_password == old


async def test_delete_connection_refuses_while_numbers_live(async_session, lk, conn):
    await _number(async_session, conn, "unverified")
    with pytest.raises(ConnectedNumberError) as e:
        await delete_connection(async_session, lk, conn)
    assert e.value.code == "has_numbers"
    lk.delete_sip_trunk.assert_not_awaited()


async def test_delete_connection_deletes_trunks_and_tombstones(async_session, lk, conn):
    await _number(async_session, conn, "released")
    await _number(async_session, conn, "failed", e164="+14155550101")
    await delete_connection(async_session, lk, conn)
    assert conn.deleted_at is not None
    assert {c.args[0] for c in lk.delete_sip_trunk.await_args_list} == {
        "ST_in",
        "ST_out",
    }


async def test_delete_connection_forgets_the_secrets(
    async_session, lk, conn_with_password
):
    await delete_connection(async_session, lk, conn_with_password)
    await async_session.refresh(conn_with_password)
    assert conn_with_password.deleted_at is not None
    assert (
        conn_with_password.encrypted_inbound_password,
        conn_with_password.pbx_username,
        conn_with_password.encrypted_pbx_password,
        conn_with_password.pbx_host,
    ) == (None, None, None, None)
    assert conn_with_password.inbound_username  # kept: unique, never reused


async def test_rotate_inbound_password_reads_the_row_under_the_lock(
    async_session, lk, conn
):
    """A connection deleted by another request after this one loaded it is
    refused before any LiveKit call."""
    async with session_scope() as other:
        await other.execute(
            update(SipConnection)
            .where(SipConnection.id == conn.id)
            .values(deleted_at=NOW)
        )
        await other.commit()
    with pytest.raises(ConnectedNumberError) as e:
        await rotate_inbound_password(async_session, lk, conn)
    assert e.value.code == "connection_deleted"
    lk.set_pbx_inbound_credentials.assert_not_awaited()


async def test_delete_connection_twice_is_refused(async_session, lk, conn):
    await delete_connection(async_session, lk, conn)
    with pytest.raises(ConnectedNumberError) as e:
        await delete_connection(async_session, lk, conn)
    assert e.value.code == "connection_deleted"


async def test_rotate_restores_old_password_when_commit_fails(
    async_session, lk, conn, monkeypatch
):
    old = decrypt_secret(conn.encrypted_inbound_password)
    username = conn.inbound_username
    monkeypatch.setattr(
        async_session, "commit", AsyncMock(side_effect=IntegrityError("x", {}, None))
    )
    with pytest.raises(IntegrityError):
        await rotate_inbound_password(async_session, lk, conn)
    calls = lk.set_pbx_inbound_credentials.await_args_list
    assert len(calls) == 2
    assert calls[1].args == ("ST_in", username, old)


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "10.0.0.5",
        "192.168.1.10",
        "169.254.169.254",
        "localhost",
        "pbx",
        "pbx.local",
        "api.internal",
        "x.svc",
        "a.cluster.local",
        "router.home.arpa",
        "a.localhost",
        "999.999.999.999",
        ("a" * 64) + ".example.com",
    ],
)
async def test_hosts_inside_a_network_are_refused(async_session, lk, conn, host):
    with pytest.raises(ConnectedNumberError) as e:
        await create_connection(
            async_session,
            lk,
            organization_id=ORG,
            label="Main office",
            default_region="US",
            pbx_host=host,
        )
    assert (e.value.code, e.value.field) == ("host_not_allowed", "pbx_host")
    with pytest.raises(ConnectedNumberError) as e:
        await update_connection(async_session, lk, conn, pbx_host=host)
    assert (e.value.code, e.value.field) == ("host_not_allowed", "pbx_host")
    lk.create_pbx_inbound_trunk.assert_not_awaited()
    lk.update_pbx_outbound_trunk.assert_not_awaited()


async def test_public_ip_host_is_accepted(async_session, lk):
    conn, _ = await create_connection(
        async_session,
        lk,
        organization_id=ORG,
        label="Main office",
        default_region="US",
        pbx_host="8.8.8.8",
    )
    assert conn.pbx_host == "8.8.8.8"


@pytest.fixture()
async def conn_with_password(async_session, lk):
    row, _ = await create_connection(
        async_session,
        lk,
        organization_id=ORG,
        label="Main office",
        default_region="US",
        pbx_host="pbx.example.com",
        pbx_username="hail",
        pbx_password="old-pw",
    )
    lk.reset_mock()
    return row


async def test_host_change_needs_password_again(async_session, lk, conn_with_password):
    with pytest.raises(ConnectedNumberError) as e:
        await update_connection(
            async_session, lk, conn_with_password, pbx_host="pbx2.example.com"
        )
    assert (e.value.code, e.value.field) == ("password_required", "pbx_password")
    lk.update_pbx_outbound_trunk.assert_not_awaited()
    lk.create_pbx_outbound_trunk.assert_not_awaited()
    await async_session.refresh(conn_with_password)  # the refusal rolled back
    assert conn_with_password.pbx_host == "pbx.example.com"


@pytest.mark.parametrize("change", [{"pbx_port": 5070}, {"pbx_transport": "tls"}])
async def test_port_or_transport_change_needs_password_again(
    async_session, lk, conn_with_password, change
):
    with pytest.raises(ConnectedNumberError) as e:
        await update_connection(async_session, lk, conn_with_password, **change)
    assert e.value.code == "password_required"


async def test_host_change_with_new_password_updates_trunk(
    async_session, lk, conn_with_password
):
    await update_connection(
        async_session,
        lk,
        conn_with_password,
        pbx_host="pbx2.example.com",
        pbx_password="new-pw",
    )
    assert lk.update_pbx_outbound_trunk.await_args.args == (
        "ST_out",
        "pbx2.example.com",
        5060,
        "auto",
        "hail",
        "new-pw",
    )


async def test_host_change_dropping_the_username_needs_no_password(
    async_session, lk, conn_with_password
):
    """No stored password goes to the new host, so none is asked for."""
    conn = await update_connection(
        async_session,
        lk,
        conn_with_password,
        pbx_host="pbx2.example.com",
        pbx_username=None,
    )
    assert lk.update_pbx_outbound_trunk.await_args.args == (
        "ST_out",
        "pbx2.example.com",
        5060,
        "auto",
        None,
        None,
    )
    assert (conn.pbx_username, conn.encrypted_pbx_password) == (None, None)


async def test_host_change_without_stored_password_is_allowed(async_session, lk, conn):
    await update_connection(async_session, lk, conn, pbx_host="pbx2.example.com")
    lk.update_pbx_outbound_trunk.assert_awaited_once()


NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


async def _seed_pbx(session, conn, e164, state, created_at):
    number = await _number(session, conn, state, e164)
    number.created_at = created_at
    await session.commit()
    return number


async def _state_of(session, number):
    return (
        await session.execute(
            select(PhoneNumber.provisioning_state)
            .where(PhoneNumber.id == number.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _count(session, e164):
    return await session.scalar(
        select(func.count()).select_from(PhoneNumber).where(PhoneNumber.e164 == e164)
    )


async def test_add_number_is_unverified_voice_only_and_on_trunk(
    async_session, lk, conn
):
    n = await add_connected_number(async_session, lk, conn, "+14155550100")
    assert (n.provider, n.provisioning_state, n.capabilities, n.is_pool) == (
        "pbx",
        "unverified",
        ["voice"],
        False,
    )
    assert (n.country_code, n.number_type) == ("US", "local")
    assert n.sip_connection_id == conn.id
    lk.add_inbound_number.assert_awaited_once_with(
        conn.livekit_inbound_trunk_id, "+14155550100"
    )


async def test_add_number_stores_e164(async_session, lk, conn):
    n = await add_connected_number(async_session, lk, conn, "+1 415 555 0100")
    assert n.e164 == "+14155550100"
    lk.add_inbound_number.assert_awaited_once_with(
        conn.livekit_inbound_trunk_id, "+14155550100"
    )
    with pytest.raises(ConnectedNumberError) as e:
        await add_connected_number(async_session, lk, conn, "+14155550100")
    assert e.value.code == "number_taken"
    assert lk.add_inbound_number.await_count == 1


async def test_add_number_commit_failure_removes_the_e164_from_the_trunk(
    async_session, lk, conn, monkeypatch
):
    trunk_id = conn.livekit_inbound_trunk_id
    monkeypatch.setattr(
        async_session, "commit", AsyncMock(side_effect=RuntimeError("commit failed"))
    )
    with pytest.raises(RuntimeError):
        await add_connected_number(async_session, lk, conn, "+1 (415) 555-0100")
    lk.remove_inbound_number.assert_awaited_once_with(trunk_id, "+14155550100")


async def test_add_number_taken_elsewhere_is_a_clean_error(async_session, lk, conn):
    async_session.add(
        PhoneNumber(
            organization_id=UUID("00000000-0000-0000-0000-0000000000b7"),
            e164="+14155550100",
            country_code="US",
            number_type="local",
            capabilities=["voice"],
            provider="twilio",
            provider_resource_id="PN_other",
            provisioning_state="active",
        )
    )
    await async_session.commit()
    with pytest.raises(ConnectedNumberError) as e:
        await add_connected_number(async_session, lk, conn, "+14155550100")
    assert e.value.code == "number_taken"
    lk.add_inbound_number.assert_not_awaited()


async def test_add_number_rejects_invalid(async_session, lk, conn):
    with pytest.raises(ConnectedNumberError) as e:
        await add_connected_number(async_session, lk, conn, "+1415")
    assert e.value.code == "invalid_number"
    assert await _count(async_session, "+1415") == 0
    lk.add_inbound_number.assert_not_awaited()


async def test_add_number_rejects_non_geographic(async_session, lk, conn):
    with pytest.raises(ConnectedNumberError) as e:
        await add_connected_number(async_session, lk, conn, "+80012345678")
    assert e.value.code == "invalid_number"
    assert await _count(async_session, "+80012345678") == 0
    lk.add_inbound_number.assert_not_awaited()


async def test_add_number_refuses_deleted_connection(async_session, lk, conn):
    conn.deleted_at = NOW
    await async_session.commit()
    with pytest.raises(ConnectedNumberError) as e:
        await add_connected_number(async_session, lk, conn, "+14155550100")
    assert e.value.code == "connection_deleted"
    lk.add_inbound_number.assert_not_awaited()


async def test_add_number_livekit_failure_leaves_no_row(async_session, lk, conn):
    lk.add_inbound_number.side_effect = RuntimeError("down")
    with pytest.raises(ConnectedNumberError) as e:
        await add_connected_number(async_session, lk, conn, "+14155550100")
    assert e.value.code == "livekit_failed"
    assert await _count(async_session, "+14155550100") == 0


async def test_add_number_commit_failure_takes_number_off_trunk(
    async_session, lk, conn, monkeypatch
):
    async def boom():
        raise RuntimeError("commit failed")

    trunk_id = conn.livekit_inbound_trunk_id
    monkeypatch.setattr(async_session, "commit", boom)
    with pytest.raises(RuntimeError):
        await add_connected_number(async_session, lk, conn, "+14155550100")
    lk.remove_inbound_number.assert_awaited_once_with(trunk_id, "+14155550100")


async def test_remove_last_number_keeps_placeholder(async_session, lk, conn):
    n = await add_connected_number(async_session, lk, conn, "+14155550100")
    await remove_connected_number(async_session, lk, n)
    lk.remove_inbound_number.assert_awaited_once_with(
        conn.livekit_inbound_trunk_id, "+14155550100"
    )
    assert n.provisioning_state == "released"
    assert n.released_at is not None


async def test_remove_number_is_idempotent(async_session, lk, conn):
    n = await add_connected_number(async_session, lk, conn, "+14155550100")
    await remove_connected_number(async_session, lk, n)
    await remove_connected_number(async_session, lk, n)
    lk.remove_inbound_number.assert_awaited_once()


async def test_remove_number_livekit_failure_keeps_row(async_session, lk, conn):
    n = await add_connected_number(async_session, lk, conn, "+14155550100")
    lk.remove_inbound_number.side_effect = RuntimeError("down")
    number_id = n.id
    with pytest.raises(ConnectedNumberError) as e:
        await remove_connected_number(async_session, lk, n)
    assert e.value.code == "livekit_failed"
    row = await async_session.get(PhoneNumber, number_id)
    assert row.provisioning_state == "unverified"


async def test_removed_number_can_be_added_again(async_session, lk, conn):
    first = await add_connected_number(async_session, lk, conn, "+14155550100")
    await remove_connected_number(async_session, lk, first)
    second = await add_connected_number(async_session, lk, conn, "+14155550100")
    assert second.id != first.id
    assert second.provisioning_state == "unverified"
    assert first.provisioning_state == "released"


async def test_stale_unverified_numbers_are_removed_after_7_days(
    async_session, lk, conn
):
    old = await _seed_pbx(
        async_session,
        conn,
        "+14155550100",
        "unverified",
        NOW - timedelta(days=7, minutes=1),
    )
    young = await _seed_pbx(
        async_session, conn, "+14155550101", "unverified", NOW - timedelta(days=6)
    )
    active = await _seed_pbx(
        async_session, conn, "+14155550102", "active", NOW - timedelta(days=30)
    )
    assert await remove_stale_unverified_numbers(lk, now=NOW) == 1
    assert [await _state_of(async_session, n) for n in (old, young, active)] == [
        "released",
        "unverified",
        "active",
    ]


async def test_stale_cleanup_continues_after_one_failure(async_session, lk, conn):
    a = await _seed_pbx(
        async_session, conn, "+14155550100", "unverified", NOW - timedelta(days=8)
    )
    b = await _seed_pbx(
        async_session, conn, "+14155550101", "unverified", NOW - timedelta(days=8)
    )
    lk.remove_inbound_number.side_effect = [RuntimeError("down"), None]
    assert await remove_stale_unverified_numbers(lk, now=NOW) == 1
    states = sorted([await _state_of(async_session, n) for n in (a, b)])
    assert states == ["released", "unverified"]


async def test_stale_cleanup_without_livekit_does_nothing(async_session, conn, caplog):
    n = await _seed_pbx(
        async_session, conn, "+14155550100", "unverified", NOW - timedelta(days=8)
    )
    with caplog.at_level("WARNING", logger="hailhq.api.connected_numbers"):
        assert await remove_stale_unverified_numbers(None, now=NOW) == 0
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    assert await _state_of(async_session, n) == "unverified"


async def test_stale_cleanup_skips_a_number_activated_before_the_lock(
    async_session, lk, conn, monkeypatch
):
    """The ownership check can activate the number between the cleanup's
    read and its lock: the cleanup must re-check the state under the lock."""
    number = await _seed_pbx(
        async_session, conn, "+14155550100", "unverified", NOW - timedelta(days=8)
    )
    real_lock = connected_numbers.org_lock

    async def activate_then_lock(db, org):
        async with session_scope() as other:
            row = await other.get(PhoneNumber, number.id)
            row.provisioning_state = "active"
            await other.commit()
        await real_lock(db, org)

    monkeypatch.setattr(connected_numbers, "org_lock", activate_then_lock)
    assert await remove_stale_unverified_numbers(lk, now=NOW) == 0
    assert await _state_of(async_session, number) == "active"
    lk.remove_inbound_number.assert_not_awaited()


async def test_remove_with_only_state_skips_other_states(async_session, lk, conn):
    number = await _number(async_session, conn, "active")
    out = await remove_connected_number(
        async_session, lk, number, only_state="unverified"
    )
    assert out.provisioning_state == "active"
    assert await _state_of(async_session, number) == "active"
    lk.remove_inbound_number.assert_not_awaited()


async def test_stale_cleanup_skips_a_number_released_before_the_lock(
    async_session, lk, conn, monkeypatch, caplog
):
    number = await _seed_pbx(
        async_session, conn, "+14155550100", "unverified", NOW - timedelta(days=8)
    )
    real_lock = connected_numbers.org_lock

    async def release_then_lock(db, org):
        async with session_scope() as other:
            row = await other.get(PhoneNumber, number.id)
            row.provisioning_state = "released"
            row.released_at = NOW
            await other.commit()
        await real_lock(db, org)

    monkeypatch.setattr(connected_numbers, "org_lock", release_then_lock)
    with caplog.at_level("WARNING", logger="hailhq.api.connected_numbers"):
        await remove_stale_unverified_numbers(lk, now=NOW)
    assert await _state_of(async_session, number) == "released"
    lk.remove_inbound_number.assert_not_awaited()
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


# --------------------------------------------------------------------------- #
# Ownership check, outbound side (Task A13)
# --------------------------------------------------------------------------- #

POOL_E164 = "+14155550199"


@pytest.fixture()
def fast_check(request, monkeypatch):
    """Short waits for every check test except the timing test."""
    if request.node.name != "test_check_timing_fits_the_window":
        monkeypatch.setattr(connected_numbers, "CHECK_LINE_SECONDS", 0.01)
        monkeypatch.setattr(connected_numbers, "CHECK_RESULT_WAIT_SECONDS", 0.5)


@pytest.fixture()
def check_lk(lk, fast_check):
    lk.create_room.return_value = "hail-check-room"
    lk.dispatch_agent.return_value = "AD_check"
    return lk


async def _fund(session, amount_cents=100_000):
    session.add(
        AccountCredit(
            organization_id=ORG,
            kind="credit",
            channel="credit",
            amount_cents=amount_cents,
            source="test.fixture",
            ref="test.fixture",
        )
    )
    await session.commit()


async def _pool(session, e164=POOL_E164):
    number = PhoneNumber(
        organization_id=None,
        e164=e164,
        country_code="US",
        number_type="local",
        capabilities=["voice"],
        provider="twilio",
        provider_resource_id="PN_pool",
        provisioning_state="active",
        is_pool=True,
    )
    session.add(number)
    await session.commit()
    return number


@pytest.fixture()
async def pbx_number(async_session, conn):
    await _fund(async_session)
    return await _number(async_session, conn, "unverified")


@pytest.fixture()
async def active_pbx_number(async_session, conn):
    await _fund(async_session)
    return await _number(async_session, conn, "active")


@pytest.fixture()
async def pool_number(async_session):
    return await _pool(async_session)


def _call_id_of(kwargs) -> UUID:
    return UUID(kwargs["participant_identity"].removeprefix("pbx-check-"))


async def _write_outcome(call_id: UUID, outcome: str, activate: bool) -> None:
    async with session_scope() as db:
        if activate:
            call = await db.get(Call, call_id)
            number = await db.get(PhoneNumber, UUID(call.metadata_[CHECK_CALL_META]))
            number.provisioning_state = "active"
        db.add(
            CallEvent(
                call_id=call_id, kind=CHECK_EVENT_KIND, payload={"outcome": outcome}
            )
        )
        await db.commit()


def inbound_says(*outcomes, activate=False, later=None, delay=0.05):
    """A ``create_sip_participant`` side effect that plays the inbound side:
    one ``pbx_check`` event per outcome at once, ``later`` after ``delay``
    seconds; ``activate`` sets the row active with the ``passed`` event."""
    tasks = []

    async def _dial(**kwargs):
        call_id = _call_id_of(kwargs)
        for outcome in outcomes:
            await _write_outcome(call_id, outcome, activate and outcome == "passed")
        if later is not None:

            async def _later():
                await asyncio.sleep(delay)
                await _write_outcome(call_id, later, activate and later == "passed")

            tasks.append(asyncio.create_task(_later()))
        return SimpleNamespace(sip_call_id="SCL_check")

    return _dial


async def usage_rows(call_id):
    async with session_scope() as db:
        return (
            (await db.execute(select(UsageEvent).where(UsageEvent.ref == str(call_id))))
            .scalars()
            .all()
        )


@pytest.fixture()
def sleeps(monkeypatch):
    seen: list[float] = []
    real = asyncio.sleep

    async def _sleep(seconds, *args, **kwargs):
        seen.append(seconds)
        return await real(seconds, *args, **kwargs)

    monkeypatch.setattr(connected_numbers.asyncio, "sleep", _sleep)
    return seen


def activates_then_releases(number):
    """A ``release_pool_reservation`` stand-in: the inbound side's pass lands
    after the poll gave up, just before the finalize commits."""
    real = connected_numbers.release_pool_reservation
    number_id = number.id

    async def _release(db, *, call_id):
        async with session_scope() as other:
            row = await other.get(PhoneNumber, number_id)
            row.provisioning_state = "active"
            other.add(
                CallEvent(
                    call_id=call_id,
                    kind=CHECK_EVENT_KIND,
                    payload={"outcome": "passed"},
                )
            )
            await other.commit()
        return await real(db, call_id=call_id)

    return _release


async def _check_call_row(
    session, pool, number_id, *, status, requested_at, e164, end_reason=None
):
    if end_reason is None and status == "completed":
        end_reason = "normal_hangup"
    call = Call(
        organization_id=ORG,
        from_number_id=pool.id,
        from_e164=pool.e164,
        to_e164=e164,
        provider="twilio",
        direction="outbound",
        status=status,
        end_reason=end_reason,
        ended_at=requested_at if end_reason is not None else None,
        voice_config={},
        max_duration_seconds=90,
        requested_at=requested_at,
        metadata_={CHECK_CALL_META: str(number_id)},
    )
    session.add(call)
    await session.commit()
    return call


@pytest.fixture()
def arrange(async_session, pbx_number, monkeypatch):
    async def _arrange(arrangement):
        now = datetime.now(timezone.utc)
        if arrangement == "empty_pool":
            return
        pool = await _pool(async_session)
        if arrangement == "check_in_flight":
            await _check_call_row(
                async_session,
                pool,
                pbx_number.id,
                status="ringing",
                requested_at=now - timedelta(seconds=10),
                e164=pbx_number.e164,
            )
        elif arrangement == "five_checks_today":
            # Other number ids: the number was removed and added again.
            for hours in (1, 2, 3, 4, 23):
                await _check_call_row(
                    async_session,
                    pool,
                    uuid4(),
                    status="completed",
                    requested_at=now - timedelta(hours=hours),
                    e164=pbx_number.e164,
                )
        elif arrangement == "no_funds":
            await async_session.execute(
                delete(AccountCredit).where(AccountCredit.organization_id == ORG)
            )
            await async_session.commit()
        elif arrangement == "premium_number":
            monkeypatch.setattr(settings, "hail_blocked_e164_prefixes", "+1415555")

    return _arrange


def test_check_timing_fits_the_window():
    assert CHECK_RESULT_WAIT_SECONDS == CHECK_CODE_SECONDS + 3
    # The outbound leg outlasts the whole line the voicebot may say.
    assert CHECK_LINE_SECONDS >= PBX_CHECK_LINE_SECONDS + 2
    assert (
        CHECK_RING_SECONDS + CHECK_RESULT_WAIT_SECONDS + CHECK_LINE_SECONDS
        < CHECK_WINDOW.total_seconds()
    )
    assert CHECK_MAX_DURATION_SECONDS == CHECK_WINDOW.total_seconds() == 90


async def test_check_passes_and_bills_one_answered_call(
    async_session, check_lk, pbx_number, pool_number
):
    lk = check_lk
    lk.create_sip_participant.side_effect = inbound_says(
        "arrived", "passed", activate=True
    )
    result = await check_connected_number(async_session, lk, pbx_number)
    assert (result.state, result.reason) == ("active", None)
    call = await async_session.get(Call, result.call_id)
    await async_session.refresh(call)
    assert call.status == "completed"
    assert call.end_reason == "normal_hangup"
    assert call.answered_at is not None
    assert call.max_duration_seconds == 90
    assert call.direction == "outbound"
    assert call.from_number_id == pool_number.id
    assert call.to_e164 == pbx_number.e164
    assert call.metadata_["pbx_check_number_id"] == str(pbx_number.id)
    assert call.metadata_[CALL_META_FROM_POOL] is True
    assert call.metadata_[CALL_META_BILLED] is True
    usage = await usage_rows(call.id)
    assert len(usage) == 1
    assert usage[0].channel == "voice"
    assert usage[0].units > 0
    await async_session.refresh(pool_number)
    assert pool_number.reserved_call_id is None
    lk.create_sip_participant.assert_awaited_once()
    kwargs = lk.create_sip_participant.await_args.kwargs
    assert kwargs["ringing_timeout_seconds"] == 30
    assert kwargs["wait_until_answered"] is True
    assert kwargs["from_e164"] == POOL_E164
    assert kwargs["to_e164"] == pbx_number.e164
    lk.delete_room.assert_awaited_once_with("hail-check-room")


async def test_check_sends_no_customer_webhook(
    async_session, check_lk, pbx_number, pool_number
):
    async_session.add(
        WebhookSubscription(
            organization_id=ORG,
            target_url="https://example.com/hook",
            secret_encrypted="x",
            status="active",
            event_types=["call.completed", "call.failed", "call.no_answer"],
        )
    )
    await async_session.commit()
    check_lk.create_sip_participant.side_effect = inbound_says(
        "arrived", "passed", activate=True
    )
    await check_connected_number(async_session, check_lk, pbx_number)
    assert (
        await async_session.scalar(select(func.count()).select_from(WebhookDelivery))
        == 0
    )


async def test_check_notifies_the_rater_after_the_commit(
    async_session, check_lk, pbx_number, pool_number, monkeypatch
):
    seen = []
    monkeypatch.setattr(connected_numbers, "notify_usage_event_recorded", seen.append)
    result = await check_connected_number(async_session, check_lk, pbx_number)
    usage = await usage_rows(result.call_id)
    assert seen == [str(usage[0].id)]


async def test_code_is_never_stored_in_clear(
    async_session, check_lk, pbx_number, pool_number, monkeypatch, caplog
):
    monkeypatch.setattr(connected_numbers.secrets, "randbelow", lambda n: 482913)
    check_lk.create_sip_participant.side_effect = inbound_says("arrived")
    with caplog.at_level("DEBUG"):
        result = await check_connected_number(async_session, check_lk, pbx_number)
    call = await async_session.get(Call, result.call_id)
    assert "482913" not in json.dumps(call.metadata_)
    assert call.metadata_[CHECK_CODE_META] == hash_check_code("482913")
    events = (
        (
            await async_session.execute(
                select(CallEvent.payload).where(CallEvent.call_id == call.id)
            )
        )
        .scalars()
        .all()
    )
    assert events
    assert "482913" not in json.dumps(events)
    assert not [r for r in caplog.records if "482913" in r.getMessage()]


async def test_code_is_six_digits(async_session, check_lk, pbx_number, pool_number):
    await check_connected_number(async_session, check_lk, pbx_number)
    code = check_lk.dispatch_agent.await_args.kwargs["metadata"]["pbx_check_send"]
    assert len(code) == 6 and code.isdigit()


async def test_check_sends_code_before_the_dial(
    async_session, check_lk, pbx_number, pool_number, monkeypatch
):
    lk = check_lk
    monkeypatch.setattr(connected_numbers.secrets, "randbelow", lambda n: 482913)
    result = await check_connected_number(async_session, lk, pbx_number)
    metadata = lk.dispatch_agent.await_args.kwargs["metadata"]
    assert metadata == {"call_id": str(result.call_id), "pbx_check_send": "482913"}
    assert lk.dispatch_agent.await_args.kwargs["agent_name"] == "hail-voicebot"
    assert [
        c[0]
        for c in lk.mock_calls
        if c[0] in ("dispatch_agent", "create_sip_participant")
    ] == ["dispatch_agent", "create_sip_participant"]


async def test_check_passes_with_the_real_inbound_side(
    async_session, check_lk, pbx_number, pool_number, conn
):
    """The real A12 inbound side, not a fake: the INVITE on the connection's
    trunk is a check in flight, and the code from the dispatch activates."""
    lk = check_lk
    number_id, e164 = pbx_number.id, pbx_number.e164

    async def _dial(**kwargs):
        call_id = _call_id_of(kwargs)
        code = lk.dispatch_agent.await_args.kwargs["metadata"]["pbx_check_send"]
        async with session_scope() as db:
            check = await check_in_flight(db, number_id, datetime.now(timezone.utc))
            assert check is not None and check.id == call_id
            arrived = await open_inbound_call(
                db,
                SipAttributes(
                    dialed=e164,
                    caller=POOL_E164,
                    trunk_id=conn.livekit_inbound_trunk_id,
                    room_name="hail-in-check",
                    provider_call_sid=None,
                ),
            )
            assert arrived == CheckArrived(number_id, call_id)
        async with session_scope() as db:
            assert await finish_check(db, arrived, code) is True
        return SimpleNamespace(sip_call_id="SCL_check")

    lk.create_sip_participant.side_effect = _dial
    result = await check_connected_number(async_session, lk, pbx_number)
    assert (result.state, result.reason) == ("active", None)
    assert await _state_of(async_session, pbx_number) == "active"


async def test_check_without_arrival_reports_no_call(
    async_session, check_lk, pbx_number, pool_number
):
    result = await check_connected_number(async_session, check_lk, pbx_number)
    assert (result.state, result.reason) == ("unverified", "no_call_arrived")
    assert len(await usage_rows(result.call_id)) == 1


async def test_check_unanswered_is_not_billed(
    async_session, check_lk, pbx_number, pool_number
):
    check_lk.create_sip_participant.side_effect = SipCallError(
        "unavailable", "no answer", status=200, metadata={"sip_status_code": "480"}
    )
    result = await check_connected_number(async_session, check_lk, pbx_number)
    assert (result.state, result.reason) == ("unverified", "no_call_arrived")
    call = await async_session.get(Call, result.call_id)
    await async_session.refresh(call)
    assert call.status == "no_answer"
    assert call.end_reason == "user_unavailable"
    assert call.answered_at is None
    assert await usage_rows(result.call_id) == []
    await async_session.refresh(pool_number)
    assert pool_number.reserved_call_id is None


async def _the_check_call(session, number_id):
    return (
        await session.execute(
            select(Call)
            .where(Call.metadata_[CHECK_CALL_META].astext == str(number_id))
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


@pytest.mark.parametrize("stage", ["carrier_route", "room_create", "agent_dispatch"])
async def test_check_setup_failure_is_livekit_failed(
    async_session, check_lk, pbx_number, pool_number, monkeypatch, stage
):
    """A Hail-side failure before the dial is not "no call arrived" and does
    not use one of the customer's daily checks."""
    if stage == "carrier_route":

        async def _no_route(db, number):
            raise ValueError("no trunk")

        monkeypatch.setattr(connected_numbers, "voice_route", _no_route)
    elif stage == "room_create":
        check_lk.create_room.side_effect = RuntimeError("livekit down")
    else:
        check_lk.dispatch_agent.side_effect = RuntimeError("livekit down")
    number_id, e164 = pbx_number.id, pbx_number.e164
    with pytest.raises(ConnectedNumberError) as e:
        await check_connected_number(async_session, check_lk, pbx_number)
    assert e.value.code == "livekit_failed"
    call = await _the_check_call(async_session, number_id)
    assert (call.status, call.end_reason) == ("failed", f"{stage}_failed")
    assert await usage_rows(call.id) == []
    check_lk.create_sip_participant.assert_not_awaited()
    if stage == "agent_dispatch":
        check_lk.delete_room.assert_awaited_once_with("hail-check-room")
    await async_session.refresh(pool_number)
    assert pool_number.reserved_call_id is None
    now = datetime.now(timezone.utc)
    assert await connected_numbers._checks_today(async_session, ORG, e164, now) == 0


async def test_checks_today_skips_only_pre_dial_failures(
    async_session, pbx_number, pool_number
):
    now = datetime.now(timezone.utc)
    reasons = {
        "carrier_route_failed": False,
        "room_create_failed": False,
        "agent_dispatch_failed": False,
        "sip_participant_failed": True,
        "sweeper_timeout": True,
        "normal_hangup": True,
        "user_unavailable": True,
    }
    for reason in reasons:
        await _check_call_row(
            async_session,
            pool_number,
            uuid4(),
            status="failed",
            requested_at=now - timedelta(hours=1),
            e164=pbx_number.e164,
            end_reason=reason,
        )
    # In flight (no end reason yet): counted.
    await _check_call_row(
        async_session,
        pool_number,
        uuid4(),
        status="ringing",
        requested_at=now - timedelta(hours=2),
        e164=pbx_number.e164,
    )
    counted = sum(reasons.values()) + 1
    assert (
        await connected_numbers._checks_today(async_session, ORG, pbx_number.e164, now)
        == counted
    )


async def test_check_dial_failure_still_counts_and_reports_no_call(
    async_session, check_lk, pbx_number, pool_number
):
    check_lk.create_sip_participant.side_effect = RuntimeError("sip down")
    result = await check_connected_number(async_session, check_lk, pbx_number)
    assert (result.state, result.reason) == ("unverified", "no_call_arrived")
    call = await _the_check_call(async_session, pbx_number.id)
    assert (call.status, call.end_reason) == ("failed", "sip_participant_failed")
    now = datetime.now(timezone.utc)
    assert (
        await connected_numbers._checks_today(async_session, ORG, pbx_number.e164, now)
        == 1
    )


async def test_check_finalizes_once(async_session, check_lk, pbx_number, pool_number):
    """A call another writer already closed (the stale-call sweeper) is not
    billed or finalized a second time."""

    async def _dial(**kwargs):
        async with session_scope() as db:
            call = await db.get(Call, _call_id_of(kwargs))
            call.status = "failed"
            call.end_reason = "sweeper_timeout"
            call.ended_at = datetime.now(timezone.utc)
            await db.commit()
        return SimpleNamespace(sip_call_id="SCL_check")

    check_lk.create_sip_participant.side_effect = _dial
    result = await check_connected_number(async_session, check_lk, pbx_number)
    call = await async_session.get(Call, result.call_id)
    await async_session.refresh(call)
    assert (call.status, call.end_reason) == ("failed", "sweeper_timeout")
    assert await usage_rows(result.call_id) == []
    await async_session.refresh(pool_number)
    assert pool_number.reserved_call_id is None


async def test_check_wrong_trunk_reports_not_authenticated(
    async_session, check_lk, pbx_number, pool_number
):
    check_lk.create_sip_participant.side_effect = inbound_says("not_authenticated")
    result = await check_connected_number(async_session, check_lk, pbx_number)
    assert (result.state, result.reason) == ("unverified", "not_authenticated")


async def test_check_code_not_received_reports_it(
    async_session, check_lk, pbx_number, pool_number
):
    check_lk.create_sip_participant.side_effect = inbound_says(
        "arrived", "code_not_received"
    )
    result = await check_connected_number(async_session, check_lk, pbx_number)
    assert (result.state, result.reason) == ("unverified", "code_not_received")


async def test_check_only_arrived_reports_code_not_received(
    async_session, check_lk, pbx_number, pool_number
):
    check_lk.create_sip_participant.side_effect = inbound_says("arrived")
    result = await check_connected_number(async_session, check_lk, pbx_number)
    assert (result.state, result.reason) == ("unverified", "code_not_received")


async def test_poll_does_not_stop_on_arrived(
    async_session, check_lk, pbx_number, pool_number, sleeps
):
    # "arrived" at the dial, "passed" 0.05 s later from a background task.
    check_lk.create_sip_participant.side_effect = inbound_says(
        "arrived", later="passed", activate=True
    )
    result = await check_connected_number(async_session, check_lk, pbx_number)
    assert result.state == "active"
    # The line only plays when the poll saw "passed".
    assert connected_numbers.CHECK_LINE_SECONDS in sleeps


async def test_late_pass_still_counts(
    async_session, check_lk, pbx_number, pool_number, monkeypatch
):
    check_lk.create_sip_participant.side_effect = inbound_says("arrived")
    monkeypatch.setattr(
        connected_numbers,
        "release_pool_reservation",
        activates_then_releases(pbx_number),
    )
    result = await check_connected_number(async_session, check_lk, pbx_number)
    assert result.state == "active"


@pytest.mark.parametrize(
    "arrangement,code",
    [
        ("check_in_flight", "check_in_progress"),
        ("five_checks_today", "check_limit"),
        ("no_funds", "no_funds"),
        ("empty_pool", "pool_exhausted"),
        ("premium_number", "not_callable"),
    ],
)
async def test_check_refused(
    async_session, check_lk, pbx_number, arrange, arrangement, code
):
    await arrange(arrangement)
    calls_before = await async_session.scalar(select(func.count()).select_from(Call))
    with pytest.raises(ConnectedNumberError) as e:
        await check_connected_number(async_session, check_lk, pbx_number)
    assert e.value.code == code
    check_lk.create_room.assert_not_awaited()
    assert (
        await async_session.scalar(select(func.count()).select_from(Call))
        == calls_before
    )
    assert (
        await async_session.scalar(
            select(func.count())
            .select_from(PhoneNumber)
            .where(PhoneNumber.reserved_call_id.is_not(None))
        )
        == 0
    )


async def test_four_checks_today_still_allowed(
    async_session, check_lk, pbx_number, pool_number
):
    now = datetime.now(timezone.utc)
    for hours in (1, 2, 3, 4):
        await _check_call_row(
            async_session,
            pool_number,
            uuid4(),
            status="completed",
            requested_at=now - timedelta(hours=hours),
            e164=pbx_number.e164,
        )
    # Older than 24 h: not counted.
    await _check_call_row(
        async_session,
        pool_number,
        pbx_number.id,
        status="completed",
        requested_at=now - timedelta(hours=25),
        e164=pbx_number.e164,
    )
    result = await check_connected_number(async_session, check_lk, pbx_number)
    assert result.call_id is not None


async def test_check_of_country_hail_does_not_sell_in_is_refused(
    async_session, check_lk, pbx_number, pool_number, monkeypatch
):
    monkeypatch.setattr(connected_numbers, "sells_in", lambda country: False)
    with pytest.raises(ConnectedNumberError) as e:
        await check_connected_number(async_session, check_lk, pbx_number)
    assert e.value.code == "not_callable"
    check_lk.create_room.assert_not_awaited()


async def test_check_with_unreadable_catalog_is_not_callable(
    async_session, check_lk, pbx_number, pool_number, monkeypatch
):
    def broken(country):
        raise FileNotFoundError("catalog")

    monkeypatch.setattr(connected_numbers, "sells_in", broken)
    with pytest.raises(ConnectedNumberError) as e:
        await check_connected_number(async_session, check_lk, pbx_number)
    assert e.value.code == "not_callable"
    check_lk.create_room.assert_not_awaited()
    assert await async_session.scalar(select(func.count()).select_from(Call)) == 0


async def test_check_without_secret_key_is_not_configured(
    async_session, check_lk, pbx_number, pool_number, monkeypatch
):
    monkeypatch.setattr(settings, "hail_provider_secret_key", None)
    with pytest.raises(ConnectedNumberError) as e:
        await check_connected_number(async_session, check_lk, pbx_number)
    assert e.value.code == "not_configured"
    check_lk.create_room.assert_not_awaited()
    assert await async_session.scalar(select(func.count()).select_from(Call)) == 0
    await async_session.refresh(pool_number)
    assert pool_number.reserved_call_id is None


async def test_check_of_released_number_is_refused(
    async_session, check_lk, conn, pool_number
):
    await _fund(async_session)
    number = await _number(async_session, conn, "released")
    with pytest.raises(ConnectedNumberError) as e:
        await check_connected_number(async_session, check_lk, number)
    assert e.value.code == "not_unverified"
    check_lk.create_room.assert_not_awaited()


async def test_check_of_active_number_does_not_dial(
    async_session, check_lk, active_pbx_number
):
    assert await check_connected_number(
        async_session, check_lk, active_pbx_number
    ) == CheckResult("active", None, None)
    check_lk.create_room.assert_not_awaited()


async def test_check_db_error_in_the_poll_still_finalizes(
    async_session, check_lk, pbx_number, pool_number, monkeypatch
):
    """A failed statement breaks the transaction: the finalize still runs on
    a clean one and frees the pool line."""

    async def _broken(db, call_id):
        await db.execute(text("select 1/0"))

    monkeypatch.setattr(connected_numbers, "check_outcomes", _broken)
    number_id = pbx_number.id
    with pytest.raises(DBAPIError):  # the result read after the finalize
        await check_connected_number(async_session, check_lk, pbx_number)
    await async_session.rollback()
    call = (
        await async_session.execute(
            select(Call)
            .where(Call.metadata_[CHECK_CALL_META].astext == str(number_id))
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    # Answered before the error: billed as an answered call.
    assert (call.status, call.end_reason) == ("completed", "normal_hangup")
    assert len(await usage_rows(call.id)) == 1
    await async_session.refresh(pool_number)
    assert pool_number.reserved_call_id is None
    check_lk.delete_room.assert_awaited_once_with("hail-check-room")


async def test_cancelled_check_frees_the_pool_line(
    async_session, check_lk, pbx_number, pool_number
):
    ringing = asyncio.Event()

    async def _dial(**kwargs):
        ringing.set()
        await asyncio.sleep(3600)

    check_lk.create_sip_participant.side_effect = _dial
    task = asyncio.create_task(
        check_connected_number(async_session, check_lk, pbx_number)
    )
    await ringing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    call = (
        await async_session.execute(
            select(Call)
            .where(Call.metadata_[CHECK_CALL_META].astext == str(pbx_number.id))
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert (call.status, call.end_reason) == ("no_answer", "user_unavailable")
    assert await usage_rows(call.id) == []
    await async_session.refresh(pool_number)
    assert pool_number.reserved_call_id is None
    check_lk.delete_room.assert_awaited_once_with("hail-check-room")


async def test_setup_failure_log_never_holds_the_code(
    async_session, check_lk, pbx_number, pool_number, monkeypatch, caplog
):
    monkeypatch.setattr(connected_numbers.secrets, "randbelow", lambda n: 482913)
    check_lk.dispatch_agent.side_effect = RuntimeError(
        "dispatch refused: {'pbx_check_send': '482913'}"
    )
    with caplog.at_level("DEBUG"), pytest.raises(ConnectedNumberError):
        await check_connected_number(async_session, check_lk, pbx_number)
    assert [r for r in caplog.records if "failed at agent_dispatch" in r.getMessage()]
    for record in caplog.records:
        assert "482913" not in record.getMessage() + (record.exc_text or "")
