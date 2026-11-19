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
    sip = FakeSip(trunk("ST_shared", "shared"))
    with pytest.raises(st.TrunkError, match="DIDWW_SIP_INBOUND_PASSWORD"):
        await st.apply(sip, DATA, SPECS["didww"], yes=False)

    monkeypatch.setattr(settings, "didww_sip_inbound_password", "pw")
    seen = []

    async def fake_auth(user, password):
        # DIDWW gets the password before LiveKit starts asking for it.
        assert st.by_name(list(sip.trunks.values()), "hail-inbound-didww") is None
        seen.append((user, password))

    monkeypatch.setattr(st.didww, "set_voice_in_trunk_auth", fake_auth)
    await st.apply(sip, DATA, SPECS["didww"], yes=True)
    created = st.by_name(list(sip.trunks.values()), "hail-inbound-didww")
    assert created.auth_username == "hail-didww"
    assert created.auth_password == "pw"
    assert seen == [("hail-didww", "pw")]


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
        sip, DATA, SPECS["telnyx"], back=False, yes=True, carriers_of=_carriers
    )
    assert list(sip.trunks["ST_tx"].numbers) == [
        "hail-placeholder-telnyx",
        "+14155550100",
    ]
    # The shared trunk keeps a placeholder, so it never matches every number.
    assert "hail-placeholder-shared" in sip.trunks["ST_shared"].numbers
    assert "+14155550101" in sip.trunks["ST_shared"].numbers
    assert any("skip +14155550199" in line for line in plan)

    await st.move(sip, DATA, SPECS["telnyx"], back=True, yes=True)
    assert list(sip.trunks["ST_tx"].numbers) == ["hail-placeholder-telnyx"]
    assert "+14155550100" in sip.trunks["ST_shared"].numbers


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


async def test_retire_shared_needs_every_carrier_key() -> None:
    sip = FakeSip(trunk("ST_shared", "shared"))
    with pytest.raises(st.TrunkError, match="LIVEKIT_TWILIO_SIP_INBOUND_TRUNK_ID"):
        await st.retire_shared(sip, DATA, SPECS, yes=False)


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
