"""Tests for scripts/ops/sip_trunks.py with a fake LiveKit SIP service."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from hailhq.core.config import settings
from livekit import api

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("sip_trunks", HERE / "sip_trunks.py")
st = importlib.util.module_from_spec(spec)
sys.modules["sip_trunks"] = st
spec.loader.exec_module(st)

DATA, SPECS = st.load()
# The repo root has no pytest config (core sets asyncio_mode there).
pytestmark = pytest.mark.asyncio


class FakeSip:
    """Keeps trunks and one dispatch rule; applies ListUpdates like LiveKit."""

    def __init__(self, *trunks, rule_trunks=("ST_shared",), fail_add=()):
        self.trunks = {t.sip_trunk_id: t for t in trunks}
        self.rule = api.SIPDispatchRuleInfo(
            sip_dispatch_rule_id="SDR_1",
            name=DATA["dispatch_rule"],
            trunk_ids=list(rule_trunks),
        )
        self.fail_add = set(fail_add)
        self.calls: list[tuple] = []

    async def list_inbound_trunk(self, _req):
        return SimpleNamespace(items=list(self.trunks.values()))

    async def list_dispatch_rule(self, _req):
        return SimpleNamespace(items=[self.rule])

    async def create_inbound_trunk(self, req):
        trunk = api.SIPInboundTrunkInfo()
        trunk.CopyFrom(req.trunk)
        trunk.sip_trunk_id = f"ST_new_{len(self.trunks)}"
        self.trunks[trunk.sip_trunk_id] = trunk
        self.calls.append(("create", trunk.name))
        return trunk

    async def update_inbound_trunk_fields(self, trunk_id, **fields):
        trunk = self.trunks[trunk_id]
        numbers = fields.get("numbers")
        if numbers is not None:
            for n in numbers.add:
                if (trunk_id, n) in self.fail_add:
                    raise RuntimeError("conflict")
                if n not in trunk.numbers:
                    trunk.numbers.append(n)
            for n in numbers.remove:
                if n in trunk.numbers:
                    trunk.numbers.remove(n)
        if fields.get("allowed_addresses") is not None:
            del trunk.allowed_addresses[:]
            trunk.allowed_addresses.extend(fields["allowed_addresses"])
        if fields.get("auth_username") is not None:
            trunk.auth_username = fields["auth_username"]
            trunk.auth_password = fields["auth_password"]
        self.calls.append(("update", trunk_id))
        return trunk

    async def update_dispatch_rule_fields(self, rule_id, *, trunk_ids):
        for t in trunk_ids.add:
            self.rule.trunk_ids.append(t)
        for t in trunk_ids.remove:
            self.rule.trunk_ids.remove(t)
        self.calls.append(("rule", rule_id))

    async def delete_trunk(self, req):
        del self.trunks[req.sip_trunk_id]
        self.calls.append(("delete", req.sip_trunk_id))


def trunk(trunk_id, name, numbers=()):
    return api.SIPInboundTrunkInfo(
        sip_trunk_id=trunk_id, name=name, numbers=list(numbers)
    )


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setattr(settings, "livekit_sip_inbound_trunk_id", "ST_shared")
    for c in st.CARRIERS:
        monkeypatch.setattr(settings, f"livekit_{c}_sip_inbound_trunk_id", "ST_shared")
    monkeypatch.setattr(settings, "didww_sip_inbound_password", "")
    monkeypatch.setattr(settings, "didww_voice_in_trunk_id", "")


async def test_dry_run_changes_nothing() -> None:
    sip = FakeSip(trunk("ST_shared", "shared", ["+14155550100"]))
    plan = await st.apply(sip, DATA, SPECS["twilio"], yes=False)
    assert sip.calls == []
    assert any(line.startswith("create trunk hail-inbound-twilio") for line in plan)


async def test_apply_creates_a_locked_trunk_with_a_placeholder() -> None:
    sip = FakeSip(trunk("ST_shared", "shared", ["+14155550100"]))
    plan = await st.apply(sip, DATA, SPECS["twilio"], yes=True)
    created = st.by_name(list(sip.trunks.values()), "hail-inbound-twilio")
    assert list(created.numbers) == ["hail-placeholder-twilio"]
    assert sorted(created.allowed_addresses) == sorted(
        SPECS["twilio"].allowed_addresses
    )
    assert created.sip_trunk_id in sip.rule.trunk_ids
    assert plan[-1] == f"set in .env: {st.env_key('twilio')}={created.sip_trunk_id}"
    # Running it again updates the same trunk, never a second one.
    await st.apply(sip, DATA, SPECS["twilio"], yes=True)
    assert len(sip.trunks) == 2


async def test_apply_only_adds_placeholders() -> None:
    """Numbers are Hail's; the shared trunk gets its placeholder so it can
    never become a trunk that matches every number. A digest user set by
    hand on the Twilio trunk is cleared: Twilio cannot answer it."""
    sip = FakeSip(
        trunk("ST_shared", "shared", ["+14155550100"]),
        trunk("ST_tw", "hail-inbound-twilio", ["hail-placeholder-twilio", "+1"]),
    )
    sip.trunks["ST_tw"].auth_username = "by-hand"
    await st.apply(sip, DATA, SPECS["twilio"], yes=True)
    assert list(sip.trunks["ST_shared"].numbers) == [
        "+14155550100",
        "hail-placeholder-shared",
    ]
    assert list(sip.trunks["ST_tw"].numbers) == ["hail-placeholder-twilio", "+1"]
    assert sip.trunks["ST_tw"].auth_username == ""


async def test_didww_needs_the_password(monkeypatch) -> None:
    sip = FakeSip(trunk("ST_shared", "shared", ["+14155550100"]))
    with pytest.raises(st.TrunkError, match="DIDWW_SIP_INBOUND_PASSWORD"):
        await st.apply(sip, DATA, SPECS["didww"], yes=False)

    monkeypatch.setattr(settings, "didww_sip_inbound_password", "pw")
    monkeypatch.setattr(settings, "didww_voice_in_trunk_id", "didww-trunk-1")
    seen = []

    async def fake_check(user, password):
        assert sip.calls == []  # read before any LiveKit change
        return {"host": "example.sip.livekit.cloud"}

    async def fake_auth(user, password, current):
        # DIDWW gets the password before LiveKit starts asking for it.
        assert st.by_name(list(sip.trunks.values()), "hail-inbound-didww") is None
        seen.append((user, password, current))

    monkeypatch.setattr(st.didww, "check_voice_in_trunk", fake_check)
    monkeypatch.setattr(st.didww, "set_voice_in_trunk_auth", fake_auth)
    await st.apply(sip, DATA, SPECS["didww"], yes=True)
    created = st.by_name(list(sip.trunks.values()), "hail-inbound-didww")
    assert created.auth_username == "hail-didww"
    assert created.auth_password == "pw"
    assert seen == [("hail-didww", "pw", {"host": "example.sip.livekit.cloud"})]


@pytest.mark.parametrize("yes", [False, True])
async def test_failed_didww_check_changes_nothing(monkeypatch, yes) -> None:
    """A DIDWW voice IN trunk that is not a SIP trunk (or a failed read)
    stops apply before the shared placeholder or any other change."""
    monkeypatch.setattr(settings, "didww_sip_inbound_password", "pw")
    monkeypatch.setattr(settings, "didww_voice_in_trunk_id", "didww-trunk-1")
    sip = FakeSip(trunk("ST_shared", "shared", ["+14155550100"]))

    async def fail_check(_user, _password):
        raise st.CarrierNotConfigured("DIDWW voice IN trunk is not a SIP trunk")

    async def no_auth(*_args):
        raise AssertionError("no DIDWW write after a failed check")

    monkeypatch.setattr(st.didww, "check_voice_in_trunk", fail_check)
    monkeypatch.setattr(st.didww, "set_voice_in_trunk_auth", no_auth)
    with pytest.raises(st.CarrierNotConfigured):
        await st.apply(sip, DATA, SPECS["didww"], yes=yes)
    assert sip.calls == []
    assert list(sip.trunks["ST_shared"].numbers) == ["+14155550100"]


async def test_didww_needs_its_trunk_id_before_any_change(monkeypatch) -> None:
    monkeypatch.setattr(settings, "didww_sip_inbound_password", "pw")
    sip = FakeSip(trunk("ST_shared", "shared", ["+14155550100"]))
    for yes in (False, True):
        with pytest.raises(st.TrunkError, match="DIDWW_VOICE_IN_TRUNK_ID"):
            await st.apply(sip, DATA, SPECS["didww"], yes=yes)
    assert sip.calls == []


async def test_apply_refuses_a_key_that_points_at_another_trunk(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "livekit_twilio_sip_inbound_trunk_id", "ST_tx")
    sip = FakeSip(
        trunk("ST_shared", "shared", ["+14155550100"]),
        trunk("ST_tw", "hail-inbound-twilio", ["hail-placeholder-twilio"]),
        trunk("ST_tx", "hail-inbound-telnyx", ["hail-placeholder-telnyx"]),
    )
    with pytest.raises(st.TrunkError, match="not the hail-inbound-twilio trunk"):
        await st.apply(sip, DATA, SPECS["twilio"], yes=True)
    assert sip.calls == []
    # A key set while no trunk has the name would create a second trunk.
    del sip.trunks["ST_tw"]
    with pytest.raises(st.TrunkError, match="not the hail-inbound-twilio trunk"):
        await st.apply(sip, DATA, SPECS["twilio"], yes=True)
    assert sip.calls == []


async def test_apply_accepts_the_matching_key(monkeypatch) -> None:
    monkeypatch.setattr(settings, "livekit_twilio_sip_inbound_trunk_id", "ST_tw")
    sip = FakeSip(
        trunk("ST_shared", "shared", ["+14155550100"]),
        trunk("ST_tw", "hail-inbound-twilio", ["hail-placeholder-twilio"]),
    )
    plan = await st.apply(sip, DATA, SPECS["twilio"], yes=True)
    assert plan[-1] == f"set in .env: {st.env_key('twilio')}=ST_tw"


@pytest.mark.parametrize(
    "error",
    [
        st.CarrierNotConfigured("DIDWW voice IN trunk is not a SIP trunk"),
        st.CarrierRequestError(422),
    ],
)
def test_main_prints_carrier_errors(monkeypatch, capsys, error) -> None:
    async def fail(_args):
        raise error

    monkeypatch.setattr(st, "run", fail)
    assert st.main(["apply", "--carrier", "didww", "--yes"]) == 2
    assert capsys.readouterr().err == f"ERROR: {error}\n"


async def test_move_needs_the_carrier_key_first() -> None:
    sip = FakeSip(trunk("ST_shared", "shared"))
    with pytest.raises(st.TrunkError, match="LIVEKIT_TELNYX_SIP_INBOUND_TRUNK_ID"):
        await st.move(sip, DATA, SPECS["telnyx"], back=False, yes=False)


async def _carriers(numbers):
    known = {"+14155550100": "telnyx", "+14155550101": "twilio"}
    return {n: known[n] for n in numbers if n in known}


async def test_move_takes_only_the_carriers_numbers(monkeypatch) -> None:
    monkeypatch.setattr(settings, "livekit_telnyx_sip_inbound_trunk_id", "ST_tx")
    sip = FakeSip(
        trunk("ST_shared", "shared", ["+14155550100", "+14155550101", "+14155550199"]),
        trunk("ST_tx", "hail-inbound-telnyx", ["hail-placeholder-telnyx"]),
    )
    plan = await st.move(
        sip,
        DATA,
        SPECS["telnyx"],
        back=False,
        yes=True,
        carriers_of=_carriers,
        drop_unknown=True,
    )
    assert list(sip.trunks["ST_tx"].numbers) == [
        "hail-placeholder-telnyx",
        "+14155550100",
    ]
    # The shared trunk keeps a placeholder, so it never matches every number.
    assert "hail-placeholder-shared" in sip.trunks["ST_shared"].numbers
    assert "+14155550101" in sip.trunks["ST_shared"].numbers
    # No live row: a released number leaves the shared trunk.
    assert "+14155550199" not in sip.trunks["ST_shared"].numbers
    assert "removed +14155550199" in plan

    monkeypatch.setattr(settings, "livekit_telnyx_sip_inbound_trunk_id", "")
    await st.move(sip, DATA, SPECS["telnyx"], back=True, yes=True)
    assert list(sip.trunks["ST_tx"].numbers) == ["hail-placeholder-telnyx"]
    assert "+14155550100" in sip.trunks["ST_shared"].numbers


async def test_move_back_finds_the_carrier_trunk_by_name_without_the_key() -> None:
    """Rollback: after the key is emptied and the services restart, numbers
    routed in between are still on the carrier trunk. A second --back moves
    them; a forward move still needs the key."""
    sip = FakeSip(
        trunk("ST_shared", "shared", ["hail-placeholder-shared"]),
        trunk(
            "ST_tx", "hail-inbound-telnyx", ["hail-placeholder-telnyx", "+14155550100"]
        ),
    )
    await st.move(sip, DATA, SPECS["telnyx"], back=True, yes=True)
    assert list(sip.trunks["ST_tx"].numbers) == ["hail-placeholder-telnyx"]
    assert "+14155550100" in sip.trunks["ST_shared"].numbers
    with pytest.raises(st.TrunkError, match="LIVEKIT_TELNYX_SIP_INBOUND_TRUNK_ID"):
        await st.move(sip, DATA, SPECS["telnyx"], back=False, yes=False)


async def test_move_back_without_the_key_needs_the_named_trunk() -> None:
    sip = FakeSip(trunk("ST_shared", "shared", ["hail-placeholder-shared"]))
    with pytest.raises(st.TrunkError, match="no LiveKit inbound trunk named"):
        await st.move(sip, DATA, SPECS["telnyx"], back=True, yes=False)
    sip = FakeSip(trunk("ST_shared", "hail-inbound-telnyx"))
    with pytest.raises(st.TrunkError, match="is the shared trunk"):
        await st.move(sip, DATA, SPECS["telnyx"], back=True, yes=False)


async def test_failed_add_puts_the_number_back(monkeypatch) -> None:
    monkeypatch.setattr(settings, "livekit_telnyx_sip_inbound_trunk_id", "ST_tx")
    sip = FakeSip(
        trunk("ST_shared", "shared", ["+14155550100"]),
        trunk("ST_tx", "hail-inbound-telnyx", ["hail-placeholder-telnyx"]),
        fail_add={("ST_tx", "+14155550100")},
    )
    with pytest.raises(RuntimeError):
        await st.move(
            sip, DATA, SPECS["telnyx"], back=False, yes=True, carriers_of=_carriers
        )
    assert "+14155550100" in sip.trunks["ST_shared"].numbers


async def test_move_refuses_a_key_that_points_at_another_trunk(monkeypatch) -> None:
    monkeypatch.setattr(settings, "livekit_telnyx_sip_inbound_trunk_id", "ST_tw")
    sip = FakeSip(
        trunk("ST_shared", "shared"),
        trunk("ST_tw", "hail-inbound-twilio", ["hail-placeholder-twilio"]),
    )
    with pytest.raises(st.TrunkError, match="not the hail-inbound-telnyx trunk"):
        await st.move(sip, DATA, SPECS["telnyx"], back=False, yes=False)


async def test_retire_shared_only_when_empty(monkeypatch) -> None:
    for i, c in enumerate(st.CARRIERS):
        monkeypatch.setattr(settings, f"livekit_{c}_sip_inbound_trunk_id", f"ST_{i}")
    sip = FakeSip(
        trunk("ST_shared", "shared", ["hail-placeholder-shared", "+14155550100"]),
        rule_trunks=("ST_shared", "ST_0", "ST_1", "ST_2"),
    )
    with pytest.raises(st.TrunkError, match="still lists 1 number"):
        await st.retire_shared(sip, DATA, SPECS, yes=True)

    sip.trunks["ST_shared"].numbers.remove("+14155550100")
    await st.retire_shared(sip, DATA, SPECS, yes=True)
    assert "ST_shared" not in sip.trunks
    assert list(sip.rule.trunk_ids) == ["ST_0", "ST_1", "ST_2"]


async def test_retire_shared_needs_the_key_of_every_carrier_in_use() -> None:
    sip = FakeSip(trunk("ST_shared", "shared"))

    async def in_use():
        return {"twilio"}

    with pytest.raises(st.TrunkError, match="LIVEKIT_TWILIO_SIP_INBOUND_TRUNK_ID"):
        await st.retire_shared(sip, DATA, SPECS, yes=False, carriers_in_use=in_use)


async def test_retire_shared_skips_a_carrier_with_no_routed_number(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "livekit_twilio_sip_inbound_trunk_id", "ST_0")
    monkeypatch.setattr(settings, "livekit_telnyx_sip_inbound_trunk_id", "ST_1")
    monkeypatch.setattr(settings, "livekit_didww_sip_inbound_trunk_id", "")
    sip = FakeSip(
        trunk("ST_shared", "shared", ["hail-placeholder-shared"]),
        rule_trunks=("ST_shared", "ST_0", "ST_1"),
    )

    async def in_use():
        return {"twilio", "telnyx"}

    with pytest.raises(st.TrunkError, match="--allow-missing"):
        await st.retire_shared(sip, DATA, SPECS, yes=True, carriers_in_use=in_use)
    await st.retire_shared(
        sip, DATA, SPECS, yes=True, carriers_in_use=in_use, allow_missing=True
    )
    assert "ST_shared" not in sip.trunks


async def test_interrupted_move_puts_the_number_back(monkeypatch) -> None:
    monkeypatch.setattr(settings, "livekit_telnyx_sip_inbound_trunk_id", "ST_tx")
    sip = FakeSip(
        trunk("ST_shared", "shared", ["+14155550100"]),
        trunk("ST_tx", "hail-inbound-telnyx", ["hail-placeholder-telnyx"]),
    )
    original = sip.update_inbound_trunk_fields

    async def ctrl_c(trunk_id, **fields):
        numbers = fields.get("numbers")
        if (
            trunk_id == "ST_tx"
            and numbers is not None
            and "+14155550100" in numbers.add
        ):
            raise KeyboardInterrupt
        return await original(trunk_id, **fields)

    sip.update_inbound_trunk_fields = ctrl_c
    with pytest.raises(KeyboardInterrupt):
        await st.move(
            sip, DATA, SPECS["telnyx"], back=False, yes=True, carriers_of=_carriers
        )
    assert "+14155550100" in sip.trunks["ST_shared"].numbers


async def test_interrupted_remove_is_found_and_put_back(monkeypatch) -> None:
    """Ctrl-C after LiveKit applied the removal: the number goes back."""
    monkeypatch.setattr(settings, "livekit_telnyx_sip_inbound_trunk_id", "ST_tx")
    sip = FakeSip(
        trunk("ST_shared", "shared", ["+14155550100"]),
        trunk("ST_tx", "hail-inbound-telnyx", ["hail-placeholder-telnyx"]),
    )
    original = sip.update_inbound_trunk_fields

    async def applied_then_cancelled(trunk_id, **fields):
        result = await original(trunk_id, **fields)
        numbers = fields.get("numbers")
        if numbers is not None and "+14155550100" in numbers.remove:
            raise KeyboardInterrupt
        return result

    sip.update_inbound_trunk_fields = applied_then_cancelled
    with pytest.raises(KeyboardInterrupt):
        await st.move(
            sip, DATA, SPECS["telnyx"], back=False, yes=True, carriers_of=_carriers
        )
    assert "+14155550100" in sip.trunks["ST_shared"].numbers


async def test_apply_skips_a_deleted_shared_trunk() -> None:
    sip = FakeSip(trunk("ST_other", "other"), rule_trunks=())
    plan = await st.apply(sip, DATA, SPECS["twilio"], yes=True)
    assert any(
        line.startswith("WARNING: LIVEKIT_SIP_INBOUND_TRUNK_ID") for line in plan
    )
    assert list(sip.trunks["ST_other"].numbers) == []


async def test_unknown_numbers_stay_unless_asked(monkeypatch) -> None:
    monkeypatch.setattr(settings, "livekit_telnyx_sip_inbound_trunk_id", "ST_tx")
    sip = FakeSip(
        trunk("ST_shared", "shared", ["+14155550100", "+14155550199"]),
        trunk("ST_tx", "hail-inbound-telnyx", ["hail-placeholder-telnyx"]),
    )
    await st.move(
        sip, DATA, SPECS["telnyx"], back=False, yes=True, carriers_of=_carriers
    )
    assert "+14155550199" in sip.trunks["ST_shared"].numbers


async def test_move_stops_when_the_database_knows_no_number(monkeypatch) -> None:
    """A laptop .env pointing at a dev database must not empty the trunk."""
    monkeypatch.setattr(settings, "livekit_telnyx_sip_inbound_trunk_id", "ST_tx")
    sip = FakeSip(
        trunk("ST_shared", "shared", ["+14155550188", "hail-placeholder-twilio"]),
        trunk("ST_tx", "hail-inbound-telnyx", ["hail-placeholder-telnyx"]),
    )
    with pytest.raises(st.TrunkError, match="DATABASE_URL"):
        await st.move(
            sip,
            DATA,
            SPECS["telnyx"],
            back=False,
            yes=True,
            carriers_of=_carriers,
            drop_unknown=True,
        )
    assert "+14155550188" in sip.trunks["ST_shared"].numbers


async def test_didww_apply_prints_the_trunk_settings_first(monkeypatch, capsys) -> None:
    """The current DIDWW settings are printed before the write, so a failed
    write still leaves them on screen. Never the password."""
    monkeypatch.setattr(settings, "didww_sip_inbound_password", "new-secret")
    monkeypatch.setattr(settings, "didww_voice_in_trunk_id", "didww-trunk-1")
    sip = FakeSip(trunk("ST_shared", "shared", ["+14155550100"]))
    current = {
        "host": "example.sip.livekit.cloud",
        "port": 5060,
        "transport_protocol_id": 2,
        "auth_enabled": True,
        "auth_user": "old-user",
        "auth_password": "old-secret",
    }

    async def fake_check(_user, _password):
        return current

    async def fail_auth(*_args):
        raise st.CarrierNotConfigured("DIDWW voice IN trunk changed its host")

    monkeypatch.setattr(st.didww, "check_voice_in_trunk", fake_check)
    monkeypatch.setattr(st.didww, "set_voice_in_trunk_auth", fail_auth)
    await st.apply(sip, DATA, SPECS["didww"], yes=False)
    with pytest.raises(st.CarrierNotConfigured):
        await st.apply(sip, DATA, SPECS["didww"], yes=True)
    out = capsys.readouterr().out
    line = (
        "DIDWW_VOICE_IN_TRUNK_ID now: host 'example.sip.livekit.cloud', "
        "port 5060, transport_protocol_id 2, auth_enabled True, "
        "auth_user 'old-user'"
    )
    assert out.count(line) == 2  # dry run, then before the failed write
    assert "secret" not in out


async def test_move_only_the_listed_numbers(monkeypatch) -> None:
    monkeypatch.setattr(settings, "livekit_telnyx_sip_inbound_trunk_id", "ST_tx")

    async def carriers(numbers):
        return {n: "telnyx" for n in numbers}

    sip = FakeSip(
        trunk("ST_shared", "shared", ["+14155550100", "+14155550102"]),
        trunk("ST_tx", "hail-inbound-telnyx", ["hail-placeholder-telnyx"]),
    )
    plan = await st.move(
        sip,
        DATA,
        SPECS["telnyx"],
        back=False,
        yes=True,
        carriers_of=carriers,
        only=["+14155550102"],
    )
    assert list(sip.trunks["ST_tx"].numbers) == [
        "hail-placeholder-telnyx",
        "+14155550102",
    ]
    assert "+14155550100" in sip.trunks["ST_shared"].numbers
    assert "moved +14155550102" in plan
    # The rest move without --number.
    await st.move(
        sip, DATA, SPECS["telnyx"], back=False, yes=True, carriers_of=carriers
    )
    assert "+14155550100" in sip.trunks["ST_tx"].numbers
    # --back --number moves only that one back.
    monkeypatch.setattr(settings, "livekit_telnyx_sip_inbound_trunk_id", "")
    await st.move(
        sip, DATA, SPECS["telnyx"], back=True, yes=True, only=["+14155550100"]
    )
    assert "+14155550100" in sip.trunks["ST_shared"].numbers
    assert "+14155550102" in sip.trunks["ST_tx"].numbers


@pytest.mark.parametrize(
    ("number", "error"),
    [
        ("+14155550177", "not on shared: \\+14155550177"),
        ("+14155550101", "not a live, routed telnyx number: \\+14155550101"),
        ("+14155550199", "not a live, routed telnyx number: \\+14155550199"),
    ],
)
@pytest.mark.parametrize("yes", [False, True])
async def test_move_refuses_a_listed_number_it_cannot_move(
    monkeypatch, number, error, yes
) -> None:
    """Not on the shared trunk, another carrier's, or no live row: nothing
    changes, not even the listed numbers that could move."""
    monkeypatch.setattr(settings, "livekit_telnyx_sip_inbound_trunk_id", "ST_tx")
    sip = FakeSip(
        trunk(
            "ST_shared",
            "shared",
            ["+14155550100", "+14155550101", "+14155550199"],
        ),
        trunk("ST_tx", "hail-inbound-telnyx", ["hail-placeholder-telnyx"]),
    )
    with pytest.raises(st.TrunkError, match=error):
        await st.move(
            sip,
            DATA,
            SPECS["telnyx"],
            back=False,
            yes=yes,
            carriers_of=_carriers,
            only=["+14155550100", number],
        )
    assert sip.calls == []


async def test_move_back_refuses_a_number_not_on_the_carrier_trunk(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "livekit_telnyx_sip_inbound_trunk_id", "ST_tx")
    sip = FakeSip(
        trunk("ST_shared", "shared", ["+14155550100"]),
        trunk("ST_tx", "hail-inbound-telnyx", ["hail-placeholder-telnyx"]),
    )
    monkeypatch.setattr(settings, "livekit_telnyx_sip_inbound_trunk_id", "")
    with pytest.raises(st.TrunkError, match="not on hail-inbound-telnyx"):
        await st.move(
            sip, DATA, SPECS["telnyx"], back=True, yes=True, only=["+14155550100"]
        )
    assert sip.calls == []


async def test_number_with_drop_unknown_removes_an_unrouted_leftover(
    monkeypatch,
) -> None:
    """A number unrouted inside the window stays on the shared trunk; it can
    be removed by name, while a live number of another carrier is refused."""
    monkeypatch.setattr(settings, "livekit_telnyx_sip_inbound_trunk_id", "ST_tx")
    sip = FakeSip(
        trunk("ST_shared", "shared", ["+14155550199", "+14155550101"]),
        trunk("ST_tx", "hail-inbound-telnyx", ["hail-placeholder-telnyx"]),
    )
    with pytest.raises(st.TrunkError, match="not a live, routed telnyx"):
        await st.move(
            sip,
            DATA,
            SPECS["telnyx"],
            back=False,
            yes=True,
            carriers_of=_carriers,
            drop_unknown=True,
            only=["+14155550101"],
        )
    plan = await st.move(
        sip,
        DATA,
        SPECS["telnyx"],
        back=False,
        yes=True,
        carriers_of=_carriers,
        drop_unknown=True,
        only=["+14155550199"],
    )
    assert "+14155550199" not in sip.trunks["ST_shared"].numbers
    assert "+14155550199" not in sip.trunks["ST_tx"].numbers
    assert "removed +14155550199" in plan


def test_main_passes_the_numbers(monkeypatch) -> None:
    seen = {}

    async def fake_run(args):
        seen["number"] = args.number
        return []

    monkeypatch.setattr(st, "run", fake_run)
    argv = ["move", "--carrier", "twilio", "--number", "+14155550100"]
    assert st.main([*argv, "--number", "+14155550101"]) == 0
    assert seen["number"] == ["+14155550100", "+14155550101"]


async def test_move_back_works_while_the_carrier_key_is_set(monkeypatch) -> None:
    """The app accepts the shared trunk while it exists, so a rollback can
    move numbers back before the key is emptied."""
    monkeypatch.setattr(settings, "livekit_telnyx_sip_inbound_trunk_id", "ST_tx")
    sip = FakeSip(
        trunk("ST_shared", "shared", ["hail-placeholder-shared"]),
        trunk("ST_tx", "hail-inbound-telnyx", ["hail-placeholder-telnyx", "+14155550100"]),
    )
    await st.move(sip, DATA, SPECS["telnyx"], back=True, yes=True)
    assert "+14155550100" in sip.trunks["ST_shared"].numbers


async def test_drop_unknown_clears_stale_entries_once_numbers_moved(
    monkeypatch,
) -> None:
    """Only stale entries left on the shared trunk: --drop-unknown removes
    them when the database knows the numbers already on the carrier trunk."""
    monkeypatch.setattr(settings, "livekit_telnyx_sip_inbound_trunk_id", "ST_tx")
    sip = FakeSip(
        trunk("ST_shared", "shared", ["hail-placeholder-shared", "+14155550199"]),
        trunk(
            "ST_tx", "hail-inbound-telnyx", ["hail-placeholder-telnyx", "+14155550100"]
        ),
    )
    plan = await st.move(
        sip,
        DATA,
        SPECS["telnyx"],
        back=False,
        yes=True,
        carriers_of=_carriers,
        drop_unknown=True,
    )
    assert "+14155550199" not in sip.trunks["ST_shared"].numbers
    assert "removed +14155550199" in plan


async def test_apply_refuses_a_shared_trunk_with_no_numbers(monkeypatch) -> None:
    """A wildcard shared trunk takes every number: adding the placeholder
    would cut calls to every number not listed on it."""
    monkeypatch.setattr(settings, "livekit_sip_inbound_trunk_id", "ST_shared")
    sip = FakeSip(trunk("ST_shared", "shared"))
    with pytest.raises(st.TrunkError, match="lists no numbers"):
        await st.apply(sip, DATA, SPECS["twilio"], yes=True)
    assert sip.calls == []


async def test_drop_unknown_keeps_a_number_routed_after_the_plan(monkeypatch) -> None:
    """The row of a number routed during the run commits after LiveKit lists
    it: the removal reads the database again and keeps it."""
    monkeypatch.setattr(settings, "livekit_telnyx_sip_inbound_trunk_id", "ST_tx")
    sip = FakeSip(
        trunk("ST_shared", "shared", ["hail-placeholder-shared", "+14155550198"]),
        trunk(
            "ST_tx", "hail-inbound-telnyx", ["hail-placeholder-telnyx", "+14155550100"]
        ),
    )
    calls = {"n": 0}

    async def carriers(numbers):
        calls["n"] += 1
        known = {"+14155550100": "telnyx"}
        if calls["n"] > 2:  # routed after the plan was made
            known["+14155550198"] = "twilio"
        return {n: known[n] for n in numbers if n in known}

    plan = await st.move(
        sip,
        DATA,
        SPECS["telnyx"],
        back=False,
        yes=True,
        carriers_of=carriers,
        drop_unknown=True,
    )
    assert "+14155550198" in sip.trunks["ST_shared"].numbers
    assert "kept +14155550198: it is routed now" in plan
