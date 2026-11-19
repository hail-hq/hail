"""Apply the per-carrier LiveKit inbound trunks and move numbers onto them.

Run from the repo root of a checkout whose ``.env`` holds the production
values (``Settings`` reads ``.env`` from the working directory):

    uv run python scripts/ops/sip_trunks.py status
    uv run python scripts/ops/sip_trunks.py apply --carrier twilio
    uv run python scripts/ops/sip_trunks.py move --carrier twilio --number +E164
    uv run python scripts/ops/sip_trunks.py move --carrier twilio
    uv run python scripts/ops/sip_trunks.py move --carrier twilio --back
    uv run python scripts/ops/sip_trunks.py retire-shared

Every command except ``status`` prints its plan and changes nothing until
``--yes`` is added.

* ``apply``: creates or updates the trunk named in
  ``infra/livekit/inbound-trunks.json`` (its allowed addresses, its digest
  username and password, a placeholder entry), adds it to the dispatch rule,
  and for DIDWW first sets the same credentials on ``DIDWW_VOICE_IN_TRUNK_ID``
  (read and checked before any change, dry run too; its current host, port,
  transport and digest user are printed first, so they can be restored by
  hand).
  Prints the trunk id for ``LIVEKIT_<CARRIER>_SIP_INBOUND_TRUNK_ID``. On the
  shared trunk it only adds the shared placeholder. It adds numbers only
  (Hail manages them).
* ``move``: moves the carrier's numbers from the shared trunk
  (``LIVEKIT_SIP_INBOUND_TRUNK_ID``) to the carrier's trunk, one at a time
  (LiveKit lists a number on one trunk only). ``--number`` (repeatable)
  moves only those numbers. ``--back`` reverses it; with the carrier key
  empty it finds the carrier trunk by its JSON name.
* ``retire-shared``: once the shared trunk holds no number, takes it out of
  the dispatch rule and deletes it.

Each trunk keeps a placeholder entry (a SIP user name no call uses): a trunk
with no numbers would match every dialed number. The JSON holds no trunk
ids and no secrets. Runbook: docs/operations/inbound-trunk-lockdown.md.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from hailhq.core.config import settings
from hailhq.core.db import dispose_engine, session_scope
from hailhq.core.models import PhoneNumber
from hailhq.core.providers.voice import didww
from hailhq.core.providers.voice.base import CarrierNotConfigured, CarrierRequestError
from livekit import api
from livekit.protocol.models import ListUpdate
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[2]
SETTINGS = ROOT / "infra" / "livekit" / "inbound-trunks.json"
CARRIERS = ("twilio", "telnyx", "didww")


class TrunkError(Exception):
    """A precondition failed. Nothing after it ran."""


@dataclass(frozen=True)
class Spec:
    carrier: str
    name: str
    placeholder: str
    allowed_addresses: list[str]
    auth_username: str


def load(path: Path = SETTINGS) -> tuple[dict, dict[str, Spec]]:
    data = json.loads(path.read_text())
    specs = {
        carrier: Spec(
            carrier=carrier,
            name=t["name"],
            placeholder=t["placeholder"],
            allowed_addresses=list(t.get("allowed_addresses") or []),
            auth_username=t.get("auth_username", ""),
        )
        for carrier, t in data["trunks"].items()
    }
    return data, specs


def env_key(carrier: str) -> str:
    return f"LIVEKIT_{carrier.upper()}_SIP_INBOUND_TRUNK_ID"


def carrier_trunk_id(carrier: str) -> str:
    """The carrier's own trunk id; ``config.py`` folds the shared id into
    blank per-carrier keys, so equal to shared means not set."""
    value = getattr(settings, f"livekit_{carrier}_sip_inbound_trunk_id")
    if not value or value == settings.livekit_sip_inbound_trunk_id:
        return ""
    return value


def password_for(spec: Spec) -> str:
    if not spec.auth_username:
        return ""
    if spec.carrier != "didww":
        raise TrunkError(f"{spec.carrier}: only DIDWW uses a digest password")
    if not settings.didww_sip_inbound_password:
        raise TrunkError("DIDWW_SIP_INBOUND_PASSWORD is empty in .env")
    return settings.didww_sip_inbound_password


async def trunks(sip) -> list:
    return list((await sip.list_inbound_trunk(api.ListSIPInboundTrunkRequest())).items)


async def trunk_by_id(sip, trunk_id: str):
    for t in await trunks(sip):
        if t.sip_trunk_id == trunk_id:
            return t
    raise TrunkError(f"no LiveKit inbound trunk {trunk_id}")


def by_name(items: list, name: str):
    found = [t for t in items if t.name == name]
    if len(found) > 1:
        raise TrunkError(f"{len(found)} LiveKit objects are named {name!r}")
    return found[0] if found else None


async def rule_named(sip, name: str):
    rules = list((await sip.list_dispatch_rule(api.ListSIPDispatchRuleRequest())).items)
    rule = by_name(rules, name)
    if rule is None:
        raise TrunkError(f"no dispatch rule named {name!r}")
    return rule


async def apply(sip, data: dict, spec: Spec, yes: bool) -> list[str]:
    """Plan, and with ``yes`` do. Returns the plan lines."""
    # Every check runs before the first change, dry run or not.
    password = password_for(spec)
    if password and not settings.didww_voice_in_trunk_id:
        raise TrunkError("DIDWW_VOICE_IN_TRUNK_ID is empty in .env")
    if spec.carrier != "didww" and not spec.allowed_addresses:
        raise TrunkError(f"{spec.carrier}: no allowed addresses in the JSON")
    rule = await rule_named(sip, data["dispatch_rule"])
    all_trunks = await trunks(sip)
    existing = by_name(all_trunks, spec.name)
    if existing is not None and (
        existing.sip_trunk_id == settings.livekit_sip_inbound_trunk_id
    ):
        raise TrunkError(f"{spec.name} is the shared trunk; rename it first")
    own = carrier_trunk_id(spec.carrier)
    if own and (existing is None or existing.sip_trunk_id != own):
        raise TrunkError(f"{env_key(spec.carrier)} is not the {spec.name} trunk")
    # DIDWW, read only: its voice IN trunk must be a SIP trunk.
    didww_sip = (
        await didww.check_voice_in_trunk(spec.auth_username, password)
        if password
        else None
    )
    if didww_sip is not None:
        # Printed at once, before any write: a failed write below would
        # lose the plan, and these are what to restore by hand.
        print(didww_settings_line(didww_sip), flush=True)
    plan: list[str] = []
    shared = settings.livekit_sip_inbound_trunk_id
    shared_trunk = next((t for t in all_trunks if t.sip_trunk_id == shared), None)
    if shared_trunk is not None and not list(shared_trunk.numbers):
        # A trunk with no numbers matches every dialed number: adding the
        # placeholder would stop calls to every number not listed on it.
        raise TrunkError(
            "the shared trunk lists no numbers (it matches every number). "
            f"Add {data['shared_placeholder']} and each routed number to it "
            "by hand first (lk sip inbound update --numbers), then run apply again"
        )
    if shared and shared_trunk is not None:
        # Emptied by unregister or move, the open shared trunk would match
        # every dialed number, including numbers on the locked trunks.
        plan.append(
            f"keep placeholder {data['shared_placeholder']} on the shared trunk"
        )
        if yes:
            await sip.update_inbound_trunk_fields(
                shared, numbers=ListUpdate(add=[data["shared_placeholder"]])
            )
    elif shared:
        plan.append(
            f"WARNING: LIVEKIT_SIP_INBOUND_TRUNK_ID {shared} is not a LiveKit "
            "trunk: no shared placeholder set. Empty the key if it was retired."
        )
    if password:
        # DIDWW first: it sends the password only when LiveKit asks. For a
        # new trunk nothing asks yet. A changed password fails DIDWW calls
        # on the existing LiveKit trunk until the update below is done.
        plan.append(
            f"set digest user {spec.auth_username} and the password on "
            "DIDWW_VOICE_IN_TRUNK_ID"
            + (
                " (a changed password fails DIDWW calls until the next step)"
                if existing is not None
                else ""
            )
        )
        if yes:
            await didww.set_voice_in_trunk_auth(spec.auth_username, password, didww_sip)
    if existing is None:
        plan.append(
            f"create trunk {spec.name}: {len(spec.allowed_addresses)} allowed "
            f"addresses, digest auth {'on' if password else 'off'}, "
            f"placeholder {spec.placeholder}"
        )
        trunk_id = ""
        if yes:
            created = await sip.create_inbound_trunk(
                api.CreateSIPInboundTrunkRequest(
                    trunk=api.SIPInboundTrunkInfo(
                        name=spec.name,
                        numbers=[spec.placeholder],
                        allowed_addresses=spec.allowed_addresses,
                        auth_username=spec.auth_username if password else "",
                        auth_password=password,
                    )
                )
            )
            trunk_id = created.sip_trunk_id
    else:
        trunk_id = existing.sip_trunk_id
        # An empty JSON list sends no update (the SDK drops an empty set),
        # so the trunk keeps what it has: say so instead of "N -> 0".
        addresses = (
            f"{len(existing.allowed_addresses)} -> {len(spec.allowed_addresses)}"
            if spec.allowed_addresses
            else f"{len(existing.allowed_addresses)} kept (the JSON lists none)"
        )
        plan.append(
            f"update trunk {spec.name} ({trunk_id}): allowed addresses "
            f"{addresses}, "
            f"digest auth {'on' if password else 'off'}, "
            f"placeholder {spec.placeholder}"
        )
        if yes:
            await sip.update_inbound_trunk_fields(
                trunk_id,
                numbers=ListUpdate(add=[spec.placeholder]),
                allowed_addresses=spec.allowed_addresses or None,
                # "" clears a digest user set by hand: Twilio and Telnyx
                # would fail its challenge.
                auth_username=spec.auth_username if password else "",
                auth_password=password,
            )
    if rule.trunk_ids and trunk_id not in rule.trunk_ids:
        plan.append(f"add trunk {spec.name} to dispatch rule {rule.name}")
        if yes:
            await sip.update_dispatch_rule_fields(
                rule.sip_dispatch_rule_id, trunk_ids=ListUpdate(add=[trunk_id])
            )
    if yes:
        plan.append(f"set in .env: {env_key(spec.carrier)}={trunk_id}")
    return plan


def didww_settings_line(sip_attrs: dict) -> str:
    """The DIDWW voice IN trunk's current settings, never its password."""
    shown = ", ".join(
        f"{key} {sip_attrs.get(key)!r}"
        for key in (
            "host",
            "port",
            "transport_protocol_id",
            "auth_enabled",
            "auth_user",
        )
    )
    return f"DIDWW_VOICE_IN_TRUNK_ID now: {shown}"


def _networks(addresses) -> set:
    """LiveKit may store ``1.2.3.4`` for ``1.2.3.4/32``: compare networks."""
    return {ipaddress.ip_network(a, strict=False) for a in addresses}


async def live_carriers(numbers: list[str]) -> dict[str, str]:
    """e164 -> carrier of the live (not released, not failed) row that Hail
    has registered for inbound calls. A number unrouted inside the window
    is left where it is."""
    if not numbers:
        return {}
    async with session_scope() as db:
        rows = await db.execute(
            select(PhoneNumber.e164, PhoneNumber.provider).where(
                PhoneNumber.e164.in_(numbers),
                PhoneNumber.provisioning_state.notin_(("released", "failed")),
                PhoneNumber.inbound_registered_at.is_not(None),
            )
        )
        return dict(rows.all())


async def move(
    sip,
    data: dict,
    spec: Spec,
    back: bool,
    yes: bool,
    carriers_of=live_carriers,
    drop_unknown: bool = False,
    only: list[str] | None = None,
) -> list[str]:
    """``only``: move just these numbers; each must be on the source trunk
    and (forward) be this carrier's live number, or nothing changes."""
    shared = settings.livekit_sip_inbound_trunk_id
    own = carrier_trunk_id(spec.carrier)
    if not shared:
        raise TrunkError("LIVEKIT_SIP_INBOUND_TRUNK_ID is empty: nothing to move")
    if own:
        own_trunk = await trunk_by_id(sip, own)
        if own_trunk.name != spec.name:
            raise TrunkError(f"{env_key(spec.carrier)} is not the {spec.name} trunk")
    elif back:
        # The second --back of a rollback runs after the key is emptied and
        # the services restart: numbers routed in between are still on the
        # carrier trunk. Find it by its JSON name.
        own_trunk = by_name(await trunks(sip), spec.name)
        if own_trunk is None:
            raise TrunkError(f"no LiveKit inbound trunk named {spec.name}")
        if own_trunk.sip_trunk_id == shared:
            raise TrunkError(f"{spec.name} is the shared trunk")
    else:
        raise TrunkError(f"set {env_key(spec.carrier)} in .env first")
    shared_trunk = await trunk_by_id(sip, shared)
    placeholders = {data["shared_placeholder"]} | {
        t["placeholder"] for t in data["trunks"].values()
    }
    src, dst = (own_trunk, shared_trunk) if back else (shared_trunk, own_trunk)
    on_src = [n for n in src.numbers if n not in placeholders]
    if only:
        absent = [n for n in only if n not in on_src]
        if absent:
            raise TrunkError(f"not on {src.name}: {', '.join(absent)}")
    candidates = list(dict.fromkeys(only)) if only else on_src
    skipped: list[str] = []
    if back:
        movable = candidates
    else:
        found = await carriers_of(candidates)
        if only:
            # With --drop-unknown, a listed number with no routed row (it was
            # unrouted inside the window) is removed from the source trunk.
            other = [
                n
                for n in candidates
                if found.get(n) != spec.carrier
                and not (drop_unknown and n not in found)
            ]
            if other:
                raise TrunkError(
                    f"not a live, routed {spec.carrier} number: {', '.join(other)}"
                    " (--drop-unknown removes one with no routed row)"
                )
            skipped = [n for n in candidates if n not in found]
        elif (
            candidates
            and not found
            and not (
                drop_unknown and await _database_knows(carriers_of, dst, placeholders)
            )
        ):
            raise TrunkError(
                f"none of the {len(candidates)} number(s) on {src.name} has a "
                "live row: is DATABASE_URL the production database? "
                "(--drop-unknown removes them once you have checked)"
            )
        movable = [n for n in candidates if found.get(n) == spec.carrier]
        if not only:
            skipped = [n for n in candidates if n not in found]
    src_placeholder = spec.placeholder if back else data["shared_placeholder"]
    plan = [
        f"keep placeholder {src_placeholder} on {src.name}",
        f"move {len(movable)} {spec.carrier} number(s) from {src.name} to {dst.name}",
    ]
    plan.extend(f"  {n}" for n in movable)
    # A released or failed number takes no calls: it belongs on no trunk.
    # Removed only on request: a wrong DATABASE_URL would list live numbers.
    action = "remove" if drop_unknown else "skip (add --drop-unknown to remove)"
    plan.extend(
        f"{action} {n} on {src.name}: no live number row (released, failed or "
        "unknown)"
        for n in skipped
    )
    if not yes:
        return plan
    # Without an entry the source trunk would match every dialed number.
    await sip.update_inbound_trunk_fields(
        src.sip_trunk_id, numbers=ListUpdate(add=[src_placeholder])
    )
    for number in movable:
        try:
            await sip.update_inbound_trunk_fields(
                src.sip_trunk_id, numbers=ListUpdate(remove=[number])
            )
            await sip.update_inbound_trunk_fields(
                dst.sip_trunk_id, numbers=ListUpdate(add=[number])
            )
        except BaseException:  # Ctrl-C too: never leave a number on no trunk
            await _recover(sip, number, src, dst)
            raise
        plan.append(f"moved {number}")
    for number in skipped if drop_unknown else []:
        # The app may have routed it since the plan: its row commits after
        # LiveKit lists it. Read again right before the removal.
        if await carriers_of([number]):
            plan.append(f"kept {number}: it is routed now")
            continue
        await sip.update_inbound_trunk_fields(
            src.sip_trunk_id, numbers=ListUpdate(remove=[number])
        )
        print(f"removed {number} from {src.name}", file=sys.stderr)
        plan.append(f"removed {number}")
    return plan


async def _recover(sip, number: str, src, dst) -> None:
    """After a failed or interrupted step, find where ``number`` is (LiveKit
    may have applied a request whose answer was lost) and put it back on the
    source trunk if it is on neither. Prints the outcome either way."""
    try:
        listed = {
            t.sip_trunk_id
            for t in await trunks(sip)
            if t.sip_trunk_id in (src.sip_trunk_id, dst.sip_trunk_id)
            and number in t.numbers
        }
        if not listed:
            await sip.update_inbound_trunk_fields(
                src.sip_trunk_id, numbers=ListUpdate(add=[number])
            )
            listed = {src.sip_trunk_id}
        where = src.name if src.sip_trunk_id in listed else dst.name
        print(f"STOPPED at {number}: it is on {where}.", file=sys.stderr)
    except BaseException:
        print(
            f"STRANDED: {number} may be on no trunk. Run status, then add it "
            f"to {src.name} by hand.",
            file=sys.stderr,
        )
        raise


async def _database_knows(carriers_of, trunk, placeholders: set[str]) -> bool:
    """True when the database knows a number already on ``trunk``: then a
    shared trunk whose numbers it does not know holds only stale entries,
    not numbers of another database (a laptop .env on a dev database)."""
    listed = [n for n in trunk.numbers if n not in placeholders]
    return bool(listed) and bool(await carriers_of(listed))


async def routed_carriers() -> set[str]:
    """Carriers with at least one number Hail has routed for inbound calls."""
    async with session_scope() as db:
        rows = await db.execute(
            select(PhoneNumber.provider)
            .where(
                PhoneNumber.provisioning_state.notin_(("released", "failed")),
                PhoneNumber.inbound_registered_at.is_not(None),
            )
            .distinct()
        )
        return set(rows.scalars().all())


async def retire_shared(
    sip,
    data: dict,
    specs: dict[str, Spec],
    yes: bool,
    carriers_in_use=routed_carriers,
    allow_missing: bool = False,
) -> list[str]:
    shared = settings.livekit_sip_inbound_trunk_id
    if not shared:
        raise TrunkError("LIVEKIT_SIP_INBOUND_TRUNK_ID is empty: nothing to retire")
    missing = [env_key(c) for c in specs if not carrier_trunk_id(c)]
    if len(missing) == len(specs):
        raise TrunkError(f"set {', '.join(missing)} in .env first")
    in_use = await carriers_in_use() if missing else set()
    blocked = [env_key(c) for c in specs if not carrier_trunk_id(c) and c in in_use]
    if blocked:
        # That carrier still falls back to the shared trunk: deleting it
        # would break its routed numbers.
        raise TrunkError(f"set {', '.join(blocked)} in .env first")
    if missing and not allow_missing:
        # Without the shared trunk that carrier has no inbound trunk at all,
        # so a number routed later fails. Say so explicitly.
        raise TrunkError(
            f"{', '.join(missing)} empty: add --allow-missing if this setup "
            "never takes inbound calls on that carrier"
        )
    if missing:
        print(
            f"warning: {', '.join(missing)} empty: that carrier gets no inbound calls",
            file=sys.stderr,
        )
    trunk = await trunk_by_id(sip, shared)
    left = [n for n in trunk.numbers if n != data["shared_placeholder"]]
    if left:
        raise TrunkError(f"{trunk.name} still lists {len(left)} number(s): move them")
    rule = await rule_named(sip, data["dispatch_rule"])
    plan = []
    if yes:
        # The app may have added a number since the check above. Checked
        # before the first change, so a refusal leaves the rule as it was.
        trunk = await trunk_by_id(sip, shared)
        left = [n for n in trunk.numbers if n != data["shared_placeholder"]]
        if left:
            raise TrunkError(f"{trunk.name} now lists {len(left)} number(s): move them")
    if shared in rule.trunk_ids:
        if len(rule.trunk_ids) == 1:
            raise TrunkError(f"{rule.name} lists only the shared trunk: run apply")
        plan.append(f"remove {trunk.name} from dispatch rule {rule.name}")
        if yes:
            await sip.update_dispatch_rule_fields(
                rule.sip_dispatch_rule_id, trunk_ids=ListUpdate(remove=[shared])
            )
    plan.append(f"delete trunk {trunk.name} ({shared})")
    if yes:
        await sip.delete_trunk(api.DeleteSIPTrunkRequest(sip_trunk_id=shared))
        plan.append("set in .env: LIVEKIT_SIP_INBOUND_TRUNK_ID= (empty)")
    return plan


async def status(sip, data: dict, specs: dict[str, Spec]) -> list[str]:
    rule = await rule_named(sip, data["dispatch_rule"])
    lines = [f"dispatch rule {rule.name}: {len(rule.trunk_ids)} trunk(s)"]
    named = {
        settings.livekit_sip_inbound_trunk_id: "shared",
        **{carrier_trunk_id(c): c for c in specs if carrier_trunk_id(c)},
    }
    placeholders = {
        data["shared_placeholder"],
        *(s.placeholder for s in specs.values()),
    }
    for t in await trunks(sip):
        role = named.get(t.sip_trunk_id, "not in .env")
        spec = specs.get(role)
        addresses = (
            "matches the JSON"
            if spec
            and _networks(t.allowed_addresses) == _networks(spec.allowed_addresses)
            else f"{len(t.allowed_addresses)}"
        )
        lines.append(
            f"{t.name} ({role}): "
            f"{len([n for n in t.numbers if n not in placeholders])} number(s), "
            f"allowed addresses {addresses}, "
            f"digest auth {'on' if t.auth_username else 'off'}, "
            f"in dispatch rule {'yes' if not rule.trunk_ids or t.sip_trunk_id in rule.trunk_ids else 'no'}"
        )
    return lines


async def run(args: argparse.Namespace) -> list[str]:
    data, specs = load(args.settings)
    lk = api.LiveKitAPI(
        settings.livekit_url, settings.livekit_api_key, settings.livekit_api_secret
    )
    try:
        if args.command == "status":
            return await status(lk.sip, data, specs)
        if args.command == "retire-shared":
            return await retire_shared(
                lk.sip, data, specs, args.yes, allow_missing=args.allow_missing
            )
        spec = specs[args.carrier]
        if args.command == "apply":
            return await apply(lk.sip, data, spec, args.yes)
        return await move(
            lk.sip,
            data,
            spec,
            args.back,
            args.yes,
            drop_unknown=args.drop_unknown,
            only=args.number,
        )
    finally:
        await lk.aclose()
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--settings", type=Path, default=SETTINGS)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    for name in ("apply", "move"):
        p = sub.add_parser(name)
        p.add_argument("--carrier", choices=CARRIERS, required=True)
        p.add_argument("--yes", action="store_true", help="make the changes")
        if name == "move":
            p.add_argument("--back", action="store_true", help="carrier -> shared")
            p.add_argument(
                "--number",
                action="append",
                metavar="+E164",
                help="move only this number (repeatable)",
            )
            p.add_argument(
                "--drop-unknown",
                action="store_true",
                help="also remove numbers with no live row from the shared trunk",
            )
    retire = sub.add_parser("retire-shared")
    retire.add_argument("--yes", action="store_true", help="make the changes")
    retire.add_argument(
        "--allow-missing",
        action="store_true",
        help="retire even if a carrier has no own trunk (it then takes no inbound calls)",
    )
    args = parser.parse_args(argv)
    try:
        lines = asyncio.run(run(args))
    except (TrunkError, CarrierNotConfigured, CarrierRequestError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print("\n".join(lines))
    if args.command != "status" and not args.yes:
        print("Dry run: nothing changed. Add --yes to make these changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
