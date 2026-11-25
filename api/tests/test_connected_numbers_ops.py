"""Operator command for connected numbers (hailhq.api.connected_numbers_ops).

The service functions, the DB session and the LiveKit client are replaced, so
no database or LiveKit call happens here."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import hailhq.api.connected_numbers_ops as ops
import pytest
from hailhq.api.connected_numbers import CheckResult, ConnectedNumberError
from hailhq.api.connected_numbers_ops import main
from hailhq.core.config import settings
from hailhq.core.livekit import LiveKitClient
from hailhq.core.secret_cipher import generate_key

ORG = str(uuid4())
CONN = str(uuid4())
NUMBER = str(uuid4())
CALL = uuid4()


def fake_conn():
    return SimpleNamespace(id=uuid4(), inbound_username="hail-user-1")


@pytest.fixture(autouse=True)
def no_io(monkeypatch):
    db = AsyncMock()
    db.get.return_value = SimpleNamespace(provider="pbx")

    @asynccontextmanager
    async def scope():
        yield db

    monkeypatch.setattr(ops, "session_scope", scope)
    monkeypatch.setattr(ops, "LiveKitClient", lambda: AsyncMock())
    return db


def test_create_prints_password_once(capsys, monkeypatch):
    conn = fake_conn()
    create = AsyncMock(return_value=(conn, "P" * 32))
    monkeypatch.setattr(ops, "create_connection", create)
    assert (
        main(
            [
                "create",
                "--org",
                ORG,
                "--label",
                "Main office",
                "--pbx-host",
                "pbx.example.com",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert out.count("P" * 32) == 1
    assert str(conn.id) in out and "hail-user-1" in out
    kwargs = create.call_args.kwargs
    assert kwargs["pbx_host"] == "pbx.example.com"
    assert kwargs["pbx_port"] == 5060 and kwargs["pbx_transport"] == "auto"


def test_check_prints_reason(capsys, monkeypatch):
    monkeypatch.setattr(
        ops,
        "check_connected_number",
        AsyncMock(return_value=CheckResult("unverified", "no_call_arrived", CALL)),
    )
    assert main(["check", "--number-id", NUMBER]) == 0
    out = capsys.readouterr().out
    assert "unverified no_call_arrived" in out and str(CALL) in out


def test_service_error_exits_1(capsys, monkeypatch):
    monkeypatch.setattr(
        ops,
        "add_connected_number",
        AsyncMock(side_effect=ConnectedNumberError("number_taken", "e164")),
    )
    assert main(["add", "--connection", CONN, "--number", "+14155550100"]) == 1
    out = capsys.readouterr().out
    assert "number_taken" in out and "e164" in out


def test_add_prints_number_id_and_state(capsys, monkeypatch):
    number = SimpleNamespace(id=uuid4(), provisioning_state="unverified")
    monkeypatch.setattr(ops, "add_connected_number", AsyncMock(return_value=number))
    assert main(["add", "--connection", CONN, "--number", "+14155550100"]) == 0
    assert f"{number.id} unverified" in capsys.readouterr().out


def test_remove_and_delete_call_the_service(monkeypatch):
    remove = AsyncMock()
    delete = AsyncMock()
    monkeypatch.setattr(ops, "remove_connected_number", remove)
    monkeypatch.setattr(ops, "delete_connection", delete)
    assert main(["remove", "--number-id", NUMBER]) == 0
    assert main(["delete", "--connection", CONN]) == 0
    remove.assert_awaited_once()
    delete.assert_awaited_once()


def test_missing_row_exits_1(capsys, no_io):
    no_io.get.return_value = None
    assert main(["delete", "--connection", CONN]) == 1
    assert "not_found" in capsys.readouterr().out


def test_bad_uuid_is_an_argument_error():
    assert main(["check", "--number-id", "nope"]) == 2


def test_dispatch_any_prints_rule_and_trunks(capsys, monkeypatch):
    clear = AsyncMock(return_value=("SDR_1", []))
    monkeypatch.setattr(
        ops,
        "LiveKitClient",
        lambda: SimpleNamespace(clear_dispatch_rule_trunks=clear, aclose=AsyncMock()),
    )
    assert main(["dispatch-any", "--rule-name", "hail-inbound"]) == 0
    clear.assert_awaited_once_with("hail-inbound")
    out = capsys.readouterr().out
    assert "SDR_1" in out and "trunks: none" in out


def test_dispatch_any_unknown_rule_exits_1(capsys, monkeypatch):
    monkeypatch.setattr(
        ops,
        "LiveKitClient",
        lambda: SimpleNamespace(
            clear_dispatch_rule_trunks=AsyncMock(return_value=None),
            aclose=AsyncMock(),
        ),
    )
    assert main(["dispatch-any", "--rule-name", "nope"]) == 1
    assert "not_found" in capsys.readouterr().out


def test_password_is_in_no_log_record(caplog, capsys, monkeypatch):
    """Real create path, LiveKit API mocked underneath."""
    monkeypatch.setattr(settings, "hail_provider_secret_key", generate_key())
    fake_api = MagicMock()
    fake_api.sip.create_inbound_trunk = AsyncMock(
        return_value=SimpleNamespace(sip_trunk_id="ST_in")
    )
    fake_api.sip.update_inbound_trunk_fields = AsyncMock()
    fake_api.aclose = AsyncMock()
    monkeypatch.setattr("hailhq.core.livekit.api.LiveKitAPI", lambda *a, **k: fake_api)
    monkeypatch.setattr(ops, "LiveKitClient", LiveKitClient)
    with caplog.at_level("DEBUG"):
        assert main(["create", "--org", ORG, "--label", "x"]) == 0
    out = capsys.readouterr().out
    password = out.split("inbound password: ")[1].split()[0]
    assert len(password) >= 24
    assert caplog.records
    for record in caplog.records:
        assert password not in record.getMessage()
    assert password not in caplog.text


@pytest.mark.parametrize(
    "released,out,code", [(True, "released", 0), (False, "not an unchecked claim", 1)]
)
def test_release_claim(capsys, monkeypatch, released, out, code):
    give_way = AsyncMock(return_value=released)
    monkeypatch.setattr(ops, "give_way_unchecked_claim", give_way)
    assert main(["release-claim", "--number", "+1 415 555 0100"]) == code
    assert out in capsys.readouterr().out
    assert give_way.await_args.args[2] == "+14155550100"


def test_release_claim_bad_number_is_an_argument_error(capsys):
    assert main(["release-claim", "--number", "4155550100x"]) == 2
    assert "not a phone number" in capsys.readouterr().err
