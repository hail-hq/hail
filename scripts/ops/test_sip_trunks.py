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
# Before the autouse no_db fixture replaces them.
REAL_LIVE_CARRIERS = st.live_carriers
REAL_LIVE_NUMBER_CARRIERS = st.live_number_carriers
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

    async def update_inbound_trunk(self, trunk_id, trunk):
        replaced = api.SIPInboundTrunkInfo()
        replaced.CopyFrom(trunk)
        replaced.sip_trunk_id = trunk_id
        self.trunks[trunk_id] = replaced
        self.calls.append(("replace", trunk_id))
        return replaced

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


SECRET = "s3cr3t-value-for-tests_0123456789abcdef"
LK = "sip:hail-test.sip.livekit.cloud;transport=tcp"


class FakeTwilio:
    """The Elastic SIP trunk's origination URLs."""

    def __init__(self, *urls):
        self.urls = {
            f"OU{i}": st.twilio.OriginationUrl(f"OU{i}", url, 10, 20, True)
            for i, url in enumerate(urls)
        }
        self.updates: list[tuple[str, str]] = []

    async def origination_urls(self):
        return list(self.urls.values())

    async def set_origination_sip_url(self, sid, sip_url):
        old = self.urls[sid]
        self.urls[sid] = st.twilio.OriginationUrl(
            sid, sip_url, old.priority, old.weight, old.enabled
        )
        self.updates.append((sid, sip_url))


@pytest.fixture(autouse=True)
def tw(monkeypatch) -> FakeTwilio:
    fake = FakeTwilio(LK)
    monkeypatch.setattr(st.twilio, "origination_urls", fake.origination_urls)
    monkeypatch.setattr(
        st.twilio, "set_origination_sip_url", fake.set_origination_sip_url
    )
    return fake


@pytest.fixture(autouse=True)
def no_db(monkeypatch):
    """apply and move read the carriers of the trunks' numbers: none known."""

    async def nobody(_numbers):
        return {}

    monkeypatch.setattr(st, "live_carriers", nobody)
    monkeypatch.setattr(st, "live_number_carriers", nobody)

    async def no_routed(_carrier):
        return []

    monkeypatch.setattr(st, "routed_numbers", no_routed)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setattr(settings, "hail_sip_inbound_secret", SECRET)
    monkeypatch.setattr(settings, "twilio_sip_trunk_sid", "TK_test")
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
        trunk(
            "ST_tx", "hail-inbound-telnyx", ["hail-placeholder-telnyx", "+14155550100"]
        ),
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


async def test_move_skips_a_number_unrouted_after_the_plan(monkeypatch) -> None:
    """The app's unregister may run between the plan and the move: the move
    reads the database again and leaves the number off the carrier trunk."""
    monkeypatch.setattr(settings, "livekit_telnyx_sip_inbound_trunk_id", "ST_tx")
    sip = FakeSip(
        trunk("ST_shared", "shared", ["hail-placeholder-shared", "+14155550100"]),
        trunk("ST_tx", "hail-inbound-telnyx", ["hail-placeholder-telnyx"]),
    )
    calls = {"n": 0}

    async def carriers(numbers):
        calls["n"] += 1
        if calls["n"] > 1:  # unrouted after the plan was made
            return {}
        return {n: "telnyx" for n in numbers}

    plan = await st.move(
        sip, DATA, SPECS["telnyx"], back=False, yes=True, carriers_of=carriers
    )
    assert list(sip.trunks["ST_tx"].numbers) == ["hail-placeholder-telnyx"]
    assert "skipped +14155550100: it is not routed now" in plan


async def test_the_json_maps_the_header_core_reads() -> None:
    from hailhq.core.inbound_calls import SIP_SECRET_ATTRIBUTE, SIP_SECRET_HEADER

    assert SPECS["twilio"].headers_to_attributes == {
        SIP_SECRET_HEADER: SIP_SECRET_ATTRIBUTE
    }
    assert not SPECS["telnyx"].headers_to_attributes
    assert not SPECS["didww"].headers_to_attributes


@pytest.mark.parametrize(
    ("url", "want"),
    [
        ("sip:lk.example.test", "sip:lk.example.test?X-Hail-Secret=v"),
        (
            "sip:lk.example.test:5061;transport=tls",
            "sip:lk.example.test:5061;transport=tls?X-Hail-Secret=v",
        ),
        (
            "sip:lk.example.test;transport=tcp?X-Other=1&x-hail-secret=old",
            "sip:lk.example.test;transport=tcp?X-Other=1&X-Hail-Secret=v",
        ),
        # Replaced where it stands; the headers after it stay after it.
        (
            "sip:lk.example.test?X-Hail-Secret=old&X-Other=1&X-Last=2",
            "sip:lk.example.test?X-Hail-Secret=v&X-Other=1&X-Last=2",
        ),
        # A repeat of the header is dropped.
        (
            "sip:lk.example.test?X-Hail-Secret=old&X-Other=1&X-Hail-Secret=v",
            "sip:lk.example.test?X-Hail-Secret=v&X-Other=1",
        ),
    ],
)
async def test_with_header_keeps_the_rest_and_replaces_an_old_value(url, want) -> None:
    assert st.with_header(url, "X-Hail-Secret", "v") == want


@pytest.mark.parametrize(
    "url",
    [
        "sip:lk.example.test?X-Hail-Secret=v&X-Other=1",
        "sip:lk.example.test;transport=tcp?x-hail-secret=v",
        "sip:lk.example.test?X-Other=1&X-HAIL-SECRET=v&X-Last=2",
    ],
)
async def test_with_header_leaves_a_matching_value_untouched(url) -> None:
    """Only the value counts: not the header's place or the name's case."""
    assert st.with_header(url, "X-Hail-Secret", "v") == url


async def test_masked_hides_only_the_secret() -> None:
    url = f"sip:lk.example.test;transport=tcp?X-Other=1&X-Hail-Secret={SECRET}"
    assert (
        st.masked(url)
        == "sip:lk.example.test;transport=tcp?X-Other=1&X-Hail-Secret=***"
    )


def _locked_twilio_trunk(monkeypatch) -> FakeSip:
    """An existing Twilio trunk with stale addresses and no header map."""
    monkeypatch.setattr(settings, "livekit_twilio_sip_inbound_trunk_id", "ST_tw")
    sip = FakeSip(
        trunk("ST_shared", "shared", ["hail-placeholder-shared", "+14155550100"]),
        trunk("ST_tw", "hail-inbound-twilio", ["hail-placeholder-twilio"]),
        rule_trunks=("ST_shared", "ST_tw"),
    )
    sip.trunks["ST_tw"].allowed_addresses.append("54.172.60.0/30")
    return sip


def _assert_addresses_only(sip: FakeSip, tw: FakeTwilio) -> None:
    """The allow-list was applied; no header map, no replace, no Twilio URL."""
    tw_trunk = sip.trunks["ST_tw"]
    assert sorted(tw_trunk.allowed_addresses) == sorted(
        SPECS["twilio"].allowed_addresses
    )
    assert not tw_trunk.headers_to_attributes
    assert ("replace", "ST_tw") not in sip.calls
    assert ("update", "ST_tw") in sip.calls
    assert tw.updates == []


async def test_twilio_apply_without_the_secret_still_sets_the_addresses(
    monkeypatch, tw
) -> None:
    """Self-hosters and the weekly IP fix are never blocked by the secret."""
    monkeypatch.setattr(settings, "hail_sip_inbound_secret", "")
    sip = _locked_twilio_trunk(monkeypatch)
    plan = await st.apply(sip, DATA, SPECS["twilio"], yes=True)
    assert "secret header: skipped, HAIL_SIP_INBOUND_SECRET is empty" in plan
    _assert_addresses_only(sip, tw)


async def test_new_twilio_trunk_without_the_secret_has_no_header_map(
    monkeypatch, tw
) -> None:
    monkeypatch.setattr(settings, "hail_sip_inbound_secret", "")
    sip = FakeSip(trunk("ST_shared", "shared", ["+14155550100"]))
    plan = await st.apply(sip, DATA, SPECS["twilio"], yes=True)
    created = st.by_name(list(sip.trunks.values()), "hail-inbound-twilio")
    assert not created.headers_to_attributes
    assert list(created.allowed_addresses) == SPECS["twilio"].allowed_addresses
    assert "secret header: skipped, HAIL_SIP_INBOUND_SECRET is empty" in plan
    assert not any("map header" in line for line in plan)
    assert tw.updates == []


async def test_twilio_apply_skips_the_header_for_a_secret_unfit_for_a_uri(
    monkeypatch, tw
) -> None:
    """A malformed secret never stops the weekly IP fix."""
    monkeypatch.setattr(settings, "hail_sip_inbound_secret", "a b&c" * 10)
    sip = _locked_twilio_trunk(monkeypatch)
    plan = await st.apply(sip, DATA, SPECS["twilio"], yes=True)
    assert (
        "secret header: skipped, HAIL_SIP_INBOUND_SECRET is not 20 or more "
        "letters, digits, - or _"
    ) in plan
    _assert_addresses_only(sip, tw)


async def test_twilio_apply_without_an_origination_url_skips_the_header(
    monkeypatch, tw
) -> None:
    tw.urls.clear()
    sip = _locked_twilio_trunk(monkeypatch)
    plan = await st.apply(sip, DATA, SPECS["twilio"], yes=True)
    assert (
        "secret header: skipped, TWILIO_SIP_TRUNK_SID has no origination URL "
        "to a LiveKit SIP host (*.sip.livekit.cloud)"
    ) in plan
    _assert_addresses_only(sip, tw)


async def test_twilio_dry_run_prints_the_url_plan_without_the_secret(tw) -> None:
    sip = FakeSip(trunk("ST_shared", "shared", ["+14155550100"]))
    plan = await st.apply(sip, DATA, SPECS["twilio"], yes=False)
    assert tw.updates == []
    assert sip.calls == []
    assert (
        f"Twilio origination URL OU0: {LK} -> {LK}?X-Hail-Secret=*** "
        "(priority 10, weight 20 kept)"
    ) in plan
    assert not any(SECRET in line for line in plan)


async def test_twilio_apply_creates_the_trunk_with_the_map_and_sets_the_url(
    tw,
) -> None:
    sip = FakeSip(trunk("ST_shared", "shared", ["+14155550100"]))
    plan = await st.apply(sip, DATA, SPECS["twilio"], yes=True)
    created = st.by_name(list(sip.trunks.values()), "hail-inbound-twilio")
    assert dict(created.headers_to_attributes) == {"X-Hail-Secret": "hail.sip_secret"}
    assert tw.updates == [("OU0", f"{LK}?X-Hail-Secret={SECRET}")]
    assert tw.urls["OU0"].priority == 10 and tw.urls["OU0"].weight == 20
    assert not any(SECRET in line for line in plan)
    # A second run changes no URL.
    plan = await st.apply(sip, DATA, SPECS["twilio"], yes=True)
    assert len(tw.updates) == 1
    assert "Twilio origination URL OU0 already sends X-Hail-Secret" in plan


async def test_twilio_apply_maps_the_header_on_an_existing_trunk_once(
    monkeypatch,
) -> None:
    """The field update has no header map: the trunk is replaced with a copy
    that keeps its numbers and addresses, and only when the map differs."""
    monkeypatch.setattr(settings, "livekit_twilio_sip_inbound_trunk_id", "ST_tw")
    sip = FakeSip(
        trunk("ST_shared", "shared", ["hail-placeholder-shared"]),
        trunk("ST_tw", "hail-inbound-twilio", ["hail-placeholder-twilio", "+1"]),
        rule_trunks=("ST_shared", "ST_tw"),
    )
    sip.trunks["ST_tw"].allowed_addresses.append("54.172.60.0/30")
    plan = await st.apply(sip, DATA, SPECS["twilio"], yes=True)
    tw_trunk = sip.trunks["ST_tw"]
    assert dict(tw_trunk.headers_to_attributes) == {"X-Hail-Secret": "hail.sip_secret"}
    assert list(tw_trunk.numbers) == ["hail-placeholder-twilio", "+1"]
    assert sorted(tw_trunk.allowed_addresses) == sorted(
        SPECS["twilio"].allowed_addresses
    )
    replaces = [c for c in sip.calls if c[0] == "replace"]
    assert replaces == [("replace", "ST_tw")]
    # The replace runs before the field update.
    assert sip.calls.index(("replace", "ST_tw")) < sip.calls.index(("update", "ST_tw"))
    assert any(line.startswith("replace trunk hail-inbound-twilio") for line in plan)
    await st.apply(sip, DATA, SPECS["twilio"], yes=True)
    assert [c for c in sip.calls if c[0] == "replace"] == [("replace", "ST_tw")]


async def test_telnyx_apply_touches_no_twilio_url(monkeypatch, tw) -> None:
    monkeypatch.setattr(settings, "hail_sip_inbound_secret", "")
    sip = FakeSip(trunk("ST_shared", "shared", ["+14155550100"]))
    await st.apply(sip, DATA, SPECS["telnyx"], yes=True)
    created = st.by_name(list(sip.trunks.values()), "hail-inbound-telnyx")
    assert not created.headers_to_attributes
    assert tw.updates == []


async def test_status_says_whether_the_secret_header_is_mapped(monkeypatch) -> None:
    monkeypatch.setattr(settings, "livekit_twilio_sip_inbound_trunk_id", "ST_tw")
    sip = FakeSip(
        trunk("ST_shared", "shared", ["hail-placeholder-shared"]),
        trunk("ST_tw", "hail-inbound-twilio", ["hail-placeholder-twilio"]),
    )
    lines = await st.status(sip, DATA, SPECS)
    assert "secret header mapped no" in next(
        line for line in lines if line.startswith("hail-inbound-twilio")
    )
    sip.trunks["ST_tw"].headers_to_attributes["X-Hail-Secret"] = "hail.sip_secret"
    lines = await st.status(sip, DATA, SPECS)
    assert "secret header mapped yes" in next(
        line for line in lines if line.startswith("hail-inbound-twilio")
    )


@pytest.mark.parametrize(
    ("url", "host"),
    [
        (LK, "hail-test.sip.livekit.cloud"),
        (
            "sips:user;x=1@Hail-Test.SIP.LiveKit.Cloud:5061;transport=tls?a=b",
            "hail-test.sip.livekit.cloud",
        ),
        ("sip:[2001:db8::1]:5060", "2001:db8::1"),
        ("sip:x.sip.livekit.cloud.evil.test", "x.sip.livekit.cloud.evil.test"),
    ],
)
async def test_sip_host(url, host) -> None:
    assert st.sip_host(url) == host


async def test_twilio_apply_skips_urls_that_are_not_livekit(tw) -> None:
    tw.urls["OU1"] = st.twilio.OriginationUrl(
        "OU1", "sip:x.sip.livekit.cloud.evil.test", 10, 10, True
    )
    sip = FakeSip(trunk("ST_shared", "shared", ["+14155550100"]))
    plan = await st.apply(sip, DATA, SPECS["twilio"], yes=True)
    assert [sid for sid, _ in tw.updates] == ["OU0"]
    assert (
        "Twilio origination URL OU1: sip:x.sip.livekit.cloud.evil.test "
        "skipped (not LiveKit). If it reaches LiveKit by IP or CNAME, "
        "Hail refuses its calls once the secret is on the VM"
    ) in plan


async def test_twilio_apply_with_no_livekit_origination_url_skips_the_header(
    monkeypatch, tw
) -> None:
    tw.urls = {
        "OU0": st.twilio.OriginationUrl("OU0", "sip:pbx.example.test", 1, 1, True)
    }
    sip = _locked_twilio_trunk(monkeypatch)
    plan = await st.apply(sip, DATA, SPECS["twilio"], yes=True)
    assert any(
        line.startswith("secret header: skipped, TWILIO_SIP_TRUNK_SID has no")
        for line in plan
    )
    _assert_addresses_only(sip, tw)


async def test_twilio_url_lines_print_before_each_write(
    monkeypatch, tw, capsys
) -> None:
    """A failed write halfway still leaves the lines of the URLs before it
    (and of the one that failed) on the screen. No secret in them."""
    tw.urls["OU1"] = st.twilio.OriginationUrl("OU1", LK + ";x=2", 10, 20, True)
    real_set = tw.set_origination_sip_url

    async def fail_second(sid, sip_url):
        if sid == "OU1":
            raise st.CarrierRequestError(500)
        await real_set(sid, sip_url)

    monkeypatch.setattr(st.twilio, "set_origination_sip_url", fail_second)
    sip = FakeSip(trunk("ST_shared", "shared", ["+14155550100"]))
    with pytest.raises(st.CarrierRequestError):
        await st.apply(sip, DATA, SPECS["twilio"], yes=True)
    out = capsys.readouterr().out
    assert out.splitlines() == [
        (
            f"Twilio origination URL OU0: {LK} -> {LK}?X-Hail-Secret=*** "
            "(priority 10, weight 20 kept)"
        ),
        (
            f"Twilio origination URL OU1: {LK};x=2 -> {LK};x=2?X-Hail-Secret=*** "
            "(priority 10, weight 20 kept)"
        ),
    ]
    assert SECRET not in out
    assert [sid for sid, _ in tw.updates] == ["OU0"]


def _twilio_on_shared(monkeypatch, carriers):
    """``carriers``: e164 -> carrier of a live row, routed or not."""

    async def found(numbers):
        return {n: carriers[n] for n in numbers if n in carriers}

    monkeypatch.setattr(st, "live_number_carriers", found)


SKIP_ON_SHARED = (
    "secret header: skipped, 1 twilio number(s) still on the shared trunk, "
    "which does not map X-Hail-Secret: run move --carrier twilio (a number "
    "not routed for inbound calls: move --carrier twilio --number <it> "
    "--drop-unknown takes it off), then apply again"
)


async def test_twilio_number_on_the_shared_trunk_skips_only_the_header_steps(
    monkeypatch, tw
) -> None:
    """The allow-list still updates; the header map and the Twilio URLs wait
    until the numbers are moved."""
    _twilio_on_shared(monkeypatch, {"+14155550100": "twilio"})
    sip = _locked_twilio_trunk(monkeypatch)
    dry = await st.apply(sip, DATA, SPECS["twilio"], yes=False)
    assert sip.calls == []
    assert SKIP_ON_SHARED in dry
    plan = await st.apply(sip, DATA, SPECS["twilio"], yes=True)
    assert SKIP_ON_SHARED in plan
    _assert_addresses_only(sip, tw)


async def test_twilio_apply_ignores_other_carriers_on_the_shared_trunk(
    monkeypatch, tw
) -> None:
    monkeypatch.setattr(settings, "livekit_twilio_sip_inbound_trunk_id", "ST_tw")
    _twilio_on_shared(monkeypatch, {"+14155550100": "telnyx"})
    sip = FakeSip(
        trunk("ST_shared", "shared", ["hail-placeholder-shared", "+14155550100"]),
        trunk("ST_tw", "hail-inbound-twilio", ["hail-placeholder-twilio"]),
        rule_trunks=("ST_shared", "ST_tw"),
    )
    await st.apply(sip, DATA, SPECS["twilio"], yes=True)
    assert [sid for sid, _ in tw.updates] == ["OU0"]


async def test_new_twilio_trunk_is_created_and_says_move_next(monkeypatch, tw) -> None:
    """move needs the trunk, so a first apply creates it (no header map) and
    says to move the numbers, then apply again."""
    _twilio_on_shared(monkeypatch, {"+14155550100": "twilio"})
    sip = FakeSip(trunk("ST_shared", "shared", ["+14155550100"]))
    plan = await st.apply(sip, DATA, SPECS["twilio"], yes=True)
    assert (
        "secret header: skipped, 1 twilio number(s) still on the shared trunk, "
        "which does not map X-Hail-Secret: set "
        "LIVEKIT_TWILIO_SIP_INBOUND_TRUNK_ID in .env, run move --carrier "
        "twilio (a number not routed for inbound calls: move --carrier twilio "
        "--number <it> --drop-unknown takes it off), then apply again"
    ) in plan
    created = st.by_name(list(sip.trunks.values()), "hail-inbound-twilio")
    assert not created.headers_to_attributes
    assert tw.updates == []


async def test_replace_keeps_other_header_maps_and_re_adds_lost_numbers(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(settings, "livekit_twilio_sip_inbound_trunk_id", "ST_tw")
    sip = FakeSip(
        trunk("ST_shared", "shared", ["hail-placeholder-shared"]),
        trunk(
            "ST_tw",
            "hail-inbound-twilio",
            ["hail-placeholder-twilio", "+14155550101", "+14155550102"],
        ),
        rule_trunks=("ST_shared", "ST_tw"),
    )
    sip.trunks["ST_tw"].headers_to_attributes["X-Other"] = "other.attr"
    sip.trunks["ST_tw"].headers_to_attributes["X-Hail-Secret"] = "stale.attr"
    real_replace = sip.update_inbound_trunk

    async def lossy_replace(trunk_id, info):
        result = await real_replace(trunk_id, info)
        sip.trunks[trunk_id].numbers.remove("+14155550102")
        return result

    sip.update_inbound_trunk = lossy_replace
    await st.apply(sip, DATA, SPECS["twilio"], yes=True)
    tw_trunk = sip.trunks["ST_tw"]
    assert dict(tw_trunk.headers_to_attributes) == {
        "X-Other": "other.attr",
        "X-Hail-Secret": "hail.sip_secret",
    }
    assert sorted(tw_trunk.numbers) == sorted(
        ["hail-placeholder-twilio", "+14155550101", "+14155550102"]
    )
    assert (
        "re-add +14155550102 to hail-inbound-twilio: the replace lost it"
        in capsys.readouterr().out
    )
    # The other map does not make every later run replace the trunk again.
    sip.calls.clear()
    await st.apply(sip, DATA, SPECS["twilio"], yes=True)
    assert not [c for c in sip.calls if c[0] == "replace"]


async def test_replace_re_adds_each_lost_number_even_when_one_is_refused(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(settings, "livekit_twilio_sip_inbound_trunk_id", "ST_tw")
    sip = FakeSip(
        trunk("ST_shared", "shared", ["hail-placeholder-shared"]),
        trunk(
            "ST_tw",
            "hail-inbound-twilio",
            ["hail-placeholder-twilio", "+14155550101", "+14155550102"],
        ),
        rule_trunks=("ST_shared", "ST_tw"),
    )
    sip.fail_add.add(("ST_tw", "+14155550101"))
    real_replace = sip.update_inbound_trunk

    async def lossy_replace(trunk_id, info):
        result = await real_replace(trunk_id, info)
        sip.trunks[trunk_id].numbers.remove("+14155550101")
        sip.trunks[trunk_id].numbers.remove("+14155550102")
        return result

    sip.update_inbound_trunk = lossy_replace
    with pytest.raises(st.TrunkError, match=r"could not re-add 1 number\(s\)"):
        await st.apply(sip, DATA, SPECS["twilio"], yes=True)
    assert "+14155550102" in sip.trunks["ST_tw"].numbers
    assert "could not re-add +14155550101 to hail-inbound-twilio" in (
        capsys.readouterr().out
    )


@pytest.mark.parametrize(
    ("url", "want"),
    [
        (
            f"sip:lk.example.test?x-hail-secret={SECRET}&X-Other=1",
            "sip:lk.example.test?x-hail-secret=***&X-Other=1",
        ),
        # Every repeat, any case, spaces around the name (as with_header reads).
        (
            (
                f"sip:lk.example.test?X-HAIL-SECRET={SECRET}&X-Other=1"
                f"& X-Hail-Secret ={SECRET}"
            ),
            "sip:lk.example.test?X-HAIL-SECRET=***&X-Other=1& X-Hail-Secret =***",
        ),
        ("sip:lk.example.test;x=1", "sip:lk.example.test;x=1"),
        (
            "sip:lk.example.test?X-Hail-Secret-Not=1",
            "sip:lk.example.test?X-Hail-Secret-Not=1",
        ),
    ],
)
async def test_masked_hides_every_secret_header_with_reads_name_rules(
    url, want
) -> None:
    assert st.masked(url) == want
    assert SECRET not in st.masked(url)


@pytest.mark.parametrize("yes", [False, True])
async def test_twilio_move_back_waits_for_the_secret_rollback(monkeypatch, yes) -> None:
    """The shared trunk does not map the header: with the secret set, Hail
    would refuse every call to a number moved back. Nothing is read or
    changed, dry run too."""
    monkeypatch.setattr(settings, "livekit_twilio_sip_inbound_trunk_id", "ST_tw")
    sip = FakeSip(
        trunk("ST_shared", "shared", ["hail-placeholder-shared"]),
        trunk("ST_tw", "hail-inbound-twilio", ["hail-placeholder-twilio", "+1"]),
    )
    with pytest.raises(st.TrunkError, match="do the Secret header rollback first"):
        await st.move(sip, DATA, SPECS["twilio"], back=True, yes=yes)
    assert sip.calls == []
    monkeypatch.setattr(settings, "hail_sip_inbound_secret", "")
    await st.move(sip, DATA, SPECS["twilio"], back=True, yes=True)
    assert "+1" in sip.trunks["ST_shared"].numbers


async def test_unrouted_twilio_number_on_the_shared_trunk_is_counted_and_listed(
    monkeypatch, tw
) -> None:
    """live_carriers (routed only) knows no number; the live Twilio row with
    no inbound route still counts. Taking it off as the hint says lets the
    next apply set the header."""
    _twilio_on_shared(monkeypatch, {"+14155550100": "twilio"})
    sip = _locked_twilio_trunk(monkeypatch)
    plan = await st.apply(sip, DATA, SPECS["twilio"], yes=True)
    assert SKIP_ON_SHARED in plan
    assert plan[plan.index(SKIP_ON_SHARED) + 1] == "  +14155550100"
    _assert_addresses_only(sip, tw)

    # The hint: move --number <it> --drop-unknown (move reads routed rows).
    async def not_routed(_numbers):
        return {}

    await st.move(
        sip,
        DATA,
        SPECS["twilio"],
        back=False,
        yes=True,
        carriers_of=not_routed,
        drop_unknown=True,
        only=["+14155550100"],
    )
    assert "+14155550100" not in sip.trunks["ST_shared"].numbers
    plan = await st.apply(sip, DATA, SPECS["twilio"], yes=True)
    assert not any(line.startswith("secret header: skipped") for line in plan)
    assert [sid for sid, _ in tw.updates] == ["OU0"]


async def test_live_number_carriers_reads_unrouted_rows_too(monkeypatch) -> None:
    seen: list[str] = []

    class FakeDb:
        async def execute(self, stmt):
            seen.append(str(stmt))
            return SimpleNamespace(all=lambda: [("+1", "twilio")])

    class Scope:
        async def __aenter__(self):
            return FakeDb()

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(st, "session_scope", Scope)
    assert await REAL_LIVE_NUMBER_CARRIERS(["+1"]) == {"+1": "twilio"}
    assert await REAL_LIVE_CARRIERS(["+1"]) == {"+1": "twilio"}
    unrouted, routed = seen
    assert "provisioning_state NOT IN" in unrouted
    assert "inbound_registered_at" not in unrouted
    assert "inbound_registered_at IS NOT NULL" in routed


@pytest.mark.parametrize(
    ("problem", "reason"),
    [
        ("no_sid", "TWILIO_SIP_TRUNK_SID is empty"),
        (
            "not_configured",
            (
                "could not read the Twilio origination URLs "
                "(TWILIO_SIP_TRUNK_SID is not set)"
            ),
        ),
        (
            "request_error",
            (
                "could not read the Twilio origination URLs (Carrier request "
                "failed with HTTP 401)"
            ),
        ),
    ],
)
async def test_failed_origination_read_skips_only_the_header_steps(
    monkeypatch, tw, problem, reason
) -> None:
    if problem == "no_sid":
        monkeypatch.setattr(settings, "twilio_sip_trunk_sid", "")
    else:
        error = (
            st.CarrierNotConfigured("TWILIO_SIP_TRUNK_SID is not set")
            if problem == "not_configured"
            else st.CarrierRequestError(401)
        )

        async def fail():
            raise error

        monkeypatch.setattr(st.twilio, "origination_urls", fail)
    sip = _locked_twilio_trunk(monkeypatch)
    plan = await st.apply(sip, DATA, SPECS["twilio"], yes=True)
    assert f"secret header: skipped, {reason}" in plan
    _assert_addresses_only(sip, tw)


async def test_replace_that_changes_another_field_stops_after_re_adding(
    monkeypatch, capsys, tw
) -> None:
    """Numbers come back first; then the changed fields are printed (the
    password by name only) and the run stops for a fix by hand."""
    monkeypatch.setattr(settings, "livekit_twilio_sip_inbound_trunk_id", "ST_tw")
    sip = FakeSip(
        trunk("ST_shared", "shared", ["hail-placeholder-shared"]),
        trunk(
            "ST_tw", "hail-inbound-twilio", ["hail-placeholder-twilio", "+14155550101"]
        ),
        rule_trunks=("ST_shared", "ST_tw"),
    )
    tw_trunk = sip.trunks["ST_tw"]
    tw_trunk.allowed_addresses.extend(["54.172.60.0/30", "54.244.51.0/30"])
    tw_trunk.auth_username = "kept-user"
    tw_trunk.auth_password = "kept-password"
    real_replace = sip.update_inbound_trunk

    async def lossy_replace(trunk_id, info):
        result = await real_replace(trunk_id, info)
        t = sip.trunks[trunk_id]
        t.numbers.remove("+14155550101")
        del t.allowed_addresses[:]
        t.auth_username = ""
        t.auth_password = ""
        return result

    sip.update_inbound_trunk = lossy_replace
    with pytest.raises(st.TrunkError, match=r"changed 3 field\(s\) of hail-inbound"):
        await st.apply(sip, DATA, SPECS["twilio"], yes=True)
    assert "+14155550101" in sip.trunks["ST_tw"].numbers
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "re-add +14155550101 to hail-inbound-twilio: the replace lost it"
    assert out[1:] == [
        (
            "hail-inbound-twilio: the replace changed allowed_addresses: "
            "['54.172.60.0/30', '54.244.51.0/30'] -> []"
        ),
        "hail-inbound-twilio: the replace changed auth_username: 'kept-user' -> ''",
        "hail-inbound-twilio: the replace changed auth_password (value hidden)",
    ]
    # Stopped before the field update and the Twilio URLs.
    assert list(sip.trunks["ST_tw"].allowed_addresses) == []
    assert tw.updates == []


async def test_trunk_changes_ignores_address_order() -> None:
    a = api.SIPInboundTrunkInfo(allowed_addresses=["1.1.1.1/32", "2.2.2.2/32"])
    b = api.SIPInboundTrunkInfo(allowed_addresses=["2.2.2.2/32", "1.1.1.1/32"])
    assert st.trunk_changes(a, b) == []
    b.headers_to_attributes["X-A"] = "a"
    assert st.trunk_changes(a, b) == ["headers_to_attributes: {} -> {'X-A': 'a'}"]


async def test_replace_puts_back_a_number_routed_during_it(monkeypatch) -> None:
    """A number Hail added between the copy and the replace is not in the
    copy; the routed numbers read from the database put it back."""
    monkeypatch.setattr(settings, "livekit_twilio_sip_inbound_trunk_id", "ST_tw")
    sip = FakeSip(
        trunk("ST_shared", "shared", ["hail-placeholder-shared"]),
        trunk("ST_tw", "hail-inbound-twilio", ["hail-placeholder-twilio"]),
        rule_trunks=("ST_shared", "ST_tw"),
    )
    sip.trunks["ST_tw"].allowed_addresses.extend(["54.172.60.0/30", "54.244.51.0/30"])

    async def routed(_carrier):
        return ["+14155550102"]

    monkeypatch.setattr(st, "routed_numbers", routed)
    await st.apply(sip, DATA, SPECS["twilio"], yes=True)
    assert "+14155550102" in sip.trunks["ST_tw"].numbers
