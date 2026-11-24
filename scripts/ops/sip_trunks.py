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
  For Twilio it also maps the ``X-Hail-Secret`` header to a participant
  attribute on the LiveKit trunk (``headers_to_attributes``), then adds
  ``?X-Hail-Secret=<HAIL_SIP_INBOUND_SECRET>`` to each origination URL of
  ``TWILIO_SIP_TRUNK_SID`` whose host is a LiveKit SIP host (read first, dry
  run too; host, transport, priority and weight kept; the secret is never
  printed). It skips these two steps, with a ``secret header: skipped``
  plan line, while ``HAIL_SIP_INBOUND_SECRET`` is empty, while a live
  Twilio number (routed or not) is still on the shared trunk (which does
  not map the header: run ``move --carrier twilio`` first; the plan lists
  them), while an origination URL already sends a different secret
  (after the Secret header rollback, add ``--rotate`` to replace it),
  while ``HAIL_SIP_INBOUND_SECRET`` is not 20 or more letters,
  digits, - or _, while ``TWILIO_SIP_TRUNK_SID`` is empty or its
  origination URLs cannot be read, or when no origination URL goes to
  LiveKit. The allowed
  addresses are applied either way. If the replace that sets the map
  changes any other field of the trunk, it prints the change and stops
  (numbers it lost are added back first, one at a time).
  Prints the trunk id for ``LIVEKIT_<CARRIER>_SIP_INBOUND_TRUNK_ID``. On the
  shared trunk it only adds the shared placeholder. It adds numbers only
  (Hail manages them).
* ``move``: moves the carrier's numbers from the shared trunk
  (``LIVEKIT_SIP_INBOUND_TRUNK_ID``) to the carrier's trunk, one at a time
  (LiveKit lists a number on one trunk only). ``--number`` (repeatable)
  moves only those numbers. ``--back`` reverses it; with the carrier key
  empty it finds the carrier trunk by its JSON name. For Twilio it refuses
  while ``HAIL_SIP_INBOUND_SECRET`` is set: do the Secret header rollback
  first.
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
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from hailhq.core.config import SIP_SECRET_PATTERN, settings
from hailhq.core.db import dispose_engine, session_scope
from hailhq.core.inbound_calls import SIP_SECRET_ATTRIBUTE, SIP_SECRET_HEADER
from hailhq.core.models import PhoneNumber
from hailhq.core.providers.voice import didww, twilio
from hailhq.core.providers.voice.base import CarrierNotConfigured, CarrierRequestError
from livekit import api
from livekit.protocol.models import ListUpdate
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[2]
SETTINGS = ROOT / "infra" / "livekit" / "inbound-trunks.json"
CARRIERS = ("twilio", "telnyx", "didww")
# Only origination URLs to these hosts get the secret header.
LIVEKIT_SIP_HOST_SUFFIX = ".sip.livekit.cloud"


class TrunkError(Exception):
    """A precondition failed. Nothing after it ran."""


@dataclass(frozen=True)
class Spec:
    carrier: str
    name: str
    placeholder: str
    allowed_addresses: list[str]
    auth_username: str
    headers_to_attributes: dict[str, str]


def load(path: Path = SETTINGS) -> tuple[dict, dict[str, Spec]]:
    data = json.loads(path.read_text())
    specs = {
        carrier: Spec(
            carrier=carrier,
            name=t["name"],
            placeholder=t["placeholder"],
            allowed_addresses=list(t.get("allowed_addresses") or []),
            auth_username=t.get("auth_username", ""),
            headers_to_attributes=dict(t.get("headers_to_attributes") or {}),
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


def secret_for(spec: Spec) -> str:
    """HAIL_SIP_INBOUND_SECRET for a trunk that maps the secret header; ""
    when it is empty. ``apply`` skips the header steps for an empty or
    malformed value."""
    if not spec.headers_to_attributes:
        return ""
    if spec.carrier != "twilio":
        raise TrunkError(f"{spec.carrier}: only Twilio sends {SIP_SECRET_HEADER}")
    return settings.hail_sip_inbound_secret


def _is_header(header: str, name: str) -> bool:
    """True when the SIP URI header ``header`` (``name=value``) is ``name``:
    spaces around it and case do not count."""
    return header.partition("=")[0].strip().lower() == name.lower()


def with_header(sip_url: str, name: str, value: str) -> str:
    """``sip_url`` with the SIP URI header ``name=value`` (after ``?``).
    Unchanged when that header already has ``value``. Otherwise a header of
    that name is replaced where it stands (any repeat of it is dropped), or
    the header is added at the end. Everything before ``?`` and the other
    headers stay as they are."""
    base, _, query = sip_url.partition("?")
    headers = [h for h in query.split("&") if h] if query else []
    same = [h.partition("=")[2] for h in headers if _is_header(h, name)]
    if same and all(unquote(v) == value for v in same):
        return sip_url
    out: list[str] = []
    placed = False
    for h in headers:
        if not _is_header(h, name):
            out.append(h)
        elif not placed:
            out.append(f"{name}={value}")
            placed = True
    if not placed:
        out.append(f"{name}={value}")
    return base + "?" + "&".join(out)


def sends_other_secret(sip_url: str, secret: str) -> bool:
    """True when ``sip_url`` already has a secret header that is not
    ``secret``."""
    has_header = masked(sip_url) != sip_url
    return has_header and with_header(sip_url, SIP_SECRET_HEADER, secret) != sip_url


def sip_host(sip_url: str) -> str:
    """The host of a SIP URI, lower case: no scheme, user, port, params or
    headers."""
    rest = re.sub(r"^sips?:", "", sip_url.strip(), flags=re.IGNORECASE)
    rest = rest.split("?", 1)[0].rsplit("@", 1)[-1].split(";", 1)[0]
    if rest.startswith("["):  # IPv6 literal
        host = rest[1:].split("]", 1)[0]
    else:
        host = rest.split(":", 1)[0]
    return host.lower().rstrip(".")


def is_livekit(sip_url: str) -> bool:
    return sip_host(sip_url).endswith(LIVEKIT_SIP_HOST_SUFFIX)


def masked(sip_url: str) -> str:
    """``sip_url`` with the value of every secret header hidden, matched by
    name as :func:`with_header` does. The rest stays as it is."""
    base, sep, query = sip_url.partition("?")
    if not sep:
        return sip_url
    out = []
    for h in query.split("&"):
        name, eq, _ = h.partition("=")
        out.append(f"{name}=***" if eq and _is_header(h, SIP_SECRET_HEADER) else h)
    return base + "?" + "&".join(out)


def header_map_text(header_map: dict[str, str]) -> str:
    return "".join(f"map header {h} to attribute {a}, " for h, a in header_map.items())


def header_map_differs(trunk, header_map: dict[str, str]) -> bool:
    """True when a header of ``header_map`` is not mapped as it says. Other
    mappings on the trunk do not count."""
    return any(trunk.headers_to_attributes.get(h) != a for h, a in header_map.items())


async def set_headers_to_attributes(
    sip,
    trunk_id: str,
    name: str,
    header_map: dict[str, str],
    routed=(),
) -> None:
    """The field update has no ``headers_to_attributes``: replace the whole
    trunk with a copy read just before. Header maps not in the JSON stay.
    Then read it again and add back, one at a time, any number of the copy
    it lost. A number LiveKit refuses, or any other field that differs from
    the copy, is printed and stops the run (:class:`TrunkError`), for the
    operator to fix by hand."""
    replace = api.SIPInboundTrunkInfo()
    replace.CopyFrom(await trunk_by_id(sip, trunk_id))
    before = list(replace.numbers)
    replace.headers_to_attributes.update(header_map)
    result = await sip.update_inbound_trunk(trunk_id, replace)
    if result.sip_trunk_id != trunk_id:
        raise TrunkError(f"LiveKit answered {result.sip_trunk_id} for {trunk_id}")
    now = await trunk_by_id(sip, trunk_id)
    after = set(now.numbers)
    # ``routed``: the carrier's routed numbers read just before, so a number
    # Hail added between the copy and the replace is put back too.
    lost = list(dict.fromkeys(n for n in [*before, *routed] if n not in after))
    failed: list[str] = []
    for number in lost:
        # One at a time: LiveKit refuses a whole add when one number is on
        # another trunk. Printed before the add, so the output names it.
        print(f"re-add {number} to {name}: the replace lost it", flush=True)
        try:
            await sip.update_inbound_trunk_fields(
                trunk_id, numbers=ListUpdate(add=[number])
            )
        except Exception as exc:
            print(f"could not re-add {number} to {name}: {exc}", flush=True)
            failed.append(number)
    changed = trunk_changes(replace, now)
    for line in changed:
        print(f"{name}: the replace changed {line}", flush=True)
    errors = []
    if failed:
        errors.append(
            f"could not re-add {len(failed)} number(s) to {name} (printed "
            "above): add them by hand (lk sip inbound update --numbers)"
        )
    if changed:
        errors.append(
            f"the replace changed {len(changed)} field(s) of {name} (printed "
            "above): restore them by hand (lk sip inbound update)"
        )
    if errors:
        raise TrunkError("; ".join(errors) + ", then run apply again")


# Not compared after a replace: numbers are re-added, LiveKit sets the times.
_NOT_COMPARED = frozenset({"numbers", "created_at", "updated_at"})
# Compared, never printed.
_HIDDEN = frozenset({"auth_password"})


def trunk_changes(sent, now) -> list[str]:
    """``field: before -> now`` for each field of ``now`` (the trunk read
    back) that differs from ``sent`` (the replace). Address lists compare
    without order. A hidden field shows only its name."""
    lines = []
    for f in sent.DESCRIPTOR.fields:
        if f.name in _NOT_COMPARED:
            continue
        old, new = getattr(sent, f.name), getattr(now, f.name)
        if f.name in ("allowed_addresses", "allowed_numbers"):
            same = sorted(old) == sorted(new)
        else:
            same = old == new
        if same:
            continue
        if f.name in _HIDDEN:
            lines.append(f"{f.name} (value hidden)")
        else:
            lines.append(f"{f.name}: {_shown(old)} -> {_shown(new)}")
    return lines


def _shown(value) -> str:
    if hasattr(value, "items"):
        return repr(dict(value))
    if hasattr(value, "ListFields"):  # a message: one line
        return "{" + str(value).replace("\n", " ").strip() + "}"
    if not isinstance(value, (str, bytes)) and hasattr(value, "__iter__"):
        return repr(list(value))
    return repr(value)


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


async def apply(
    sip, data: dict, spec: Spec, yes: bool, carriers_of=None, rotate: bool = False
) -> list[str]:
    """Plan, and with ``yes`` do. Returns the plan lines. ``carriers_of``
    defaults to :func:`live_number_carriers` (looked up at call time).
    ``rotate`` allows replacing a different secret already in an
    origination URL."""
    # Every check runs before the first change, dry run or not.
    password = password_for(spec)
    secret = secret_for(spec)
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
    # The secret header steps (map on LiveKit, origination URLs on Twilio)
    # run only when nothing below says to skip them. A skip never stops the
    # allowed addresses: self-hosters and the weekly IP fix need them.
    skip = ""
    origination: list = []
    on_shared: list[str] = []
    if spec.headers_to_attributes and not secret:
        skip = "HAIL_SIP_INBOUND_SECRET is empty"
    elif secret and not SIP_SECRET_PATTERN.fullmatch(secret):
        # It goes into a SIP URI as is. Hail turns its check off for such a
        # value (inbound_calls.py), so the header is not needed yet.
        skip = "HAIL_SIP_INBOUND_SECRET is not 20 or more letters, digits, - or _"
    elif secret:
        # Twilio numbers still on the shared trunk, routed or not (one that
        # routes later takes its calls there): it does not map the header,
        # so Hail refuses their calls once the secret is on the VM.
        if shared_trunk is not None:
            placeholders = {data["shared_placeholder"]} | {
                t["placeholder"] for t in data["trunks"].values()
            }
            listed = [n for n in shared_trunk.numbers if n not in placeholders]
            found = await (carriers_of or live_number_carriers)(listed)
            on_shared = [n for n in listed if found.get(n) == spec.carrier]
        if on_shared:
            first = "" if own else f"set {env_key(spec.carrier)} in .env, "
            skip = (
                f"{len(on_shared)} {spec.carrier} number(s) still on the shared "
                f"trunk, which does not map {SIP_SECRET_HEADER}: {first}run "
                f"move --carrier {spec.carrier} (a number not routed for "
                f"inbound calls: move --carrier {spec.carrier} --number <it> "
                "--drop-unknown takes it off), then apply again"
            )
        elif not settings.twilio_sip_trunk_sid:
            skip = "TWILIO_SIP_TRUNK_SID is empty"
        else:
            # Twilio, read only: the origination URLs to add the header to.
            # A failed read skips the header steps, never the allow-list.
            try:
                origination = await twilio.origination_urls()
            except (CarrierNotConfigured, CarrierRequestError) as exc:
                origination = []
                skip = f"could not read the Twilio origination URLs ({exc})"
            else:
                if not any(is_livekit(u.sip_url) for u in origination):
                    origination = []
                    skip = (
                        "TWILIO_SIP_TRUNK_SID has no origination URL to a "
                        f"LiveKit SIP host (*{LIVEKIT_SIP_HOST_SUFFIX})"
                    )
                elif not rotate and any(
                    is_livekit(u.sip_url) and sends_other_secret(u.sip_url, secret)
                    for u in origination
                ):
                    # Twilio would send the new value while the VM still
                    # checks the old one: every Twilio call refused.
                    origination = []
                    skip = (
                        f"an origination URL already sends a different "
                        f"{SIP_SECRET_HEADER}: do the Secret header rollback "
                        f"first, then apply --carrier {spec.carrier} --rotate"
                    )
    header_map = {} if skip else spec.headers_to_attributes
    if skip:
        plan.append(f"secret header: skipped, {skip}")
        plan.extend(f"  {n}" for n in on_shared)
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
            f"{header_map_text(header_map)}placeholder {spec.placeholder}"
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
                        headers_to_attributes=header_map,
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
        if header_map_differs(existing, header_map):
            # Before the field update, which then sets the auth fields again.
            # A number Hail adds to or removes from this trunk during the
            # replace is undone; a number of the copy that the replace drops
            # is added back.
            plan.append(
                f"replace trunk {spec.name} to "
                f"{header_map_text(header_map).removesuffix(', ')} (other header maps "
                "kept; numbers the replace drops, and routed numbers missing "
                "after it, are added back)"
            )
            if yes:
                await set_headers_to_attributes(
                    sip,
                    trunk_id,
                    spec.name,
                    header_map,
                    routed=await routed_numbers(spec.carrier),
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
    # Last, once LiveKit maps the header. Hail checks it only once
    # HAIL_SIP_INBOUND_SECRET is set on the VM.
    for url in origination:
        if not is_livekit(url.sip_url):
            plan.append(
                f"Twilio origination URL {url.sid}: {masked(url.sip_url)} "
                "skipped (not LiveKit). If it reaches LiveKit by IP or CNAME, "
                "Hail refuses its calls once the secret is on the VM"
            )
            continue
        new_url = with_header(url.sip_url, SIP_SECRET_HEADER, secret)
        if new_url == url.sip_url:
            plan.append(
                f"Twilio origination URL {url.sid} already sends {SIP_SECRET_HEADER}"
            )
            continue
        line = (
            f"Twilio origination URL {url.sid}: {masked(url.sip_url)} -> "
            f"{masked(new_url)} (priority {url.priority}, weight {url.weight} kept)"
            + (
                ": CHANGES the secret (--rotate)"
                if sends_other_secret(url.sip_url, secret)
                else ""
            )
        )
        if not yes:
            plan.append(line)
            continue
        # Printed before the write, like the DIDWW settings line: if a later
        # write fails, the output still shows which URLs changed.
        print(line, flush=True)
        await twilio.set_origination_sip_url(url.sid, new_url)
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
    return await _carriers(numbers, routed_only=True)


async def live_number_carriers(numbers: list[str]) -> dict[str, str]:
    """e164 -> carrier of the live (not released, not failed) row, routed
    for inbound calls or not: a number that routes later still takes its
    calls on the trunk it is listed on."""
    return await _carriers(numbers, routed_only=False)


async def routed_numbers(carrier: str) -> list[str]:
    """Every live number of ``carrier`` that Hail has routed for inbound
    calls: after a full trunk replace, these belong on the carrier trunk."""
    async with session_scope() as db:
        rows = await db.execute(
            select(PhoneNumber.e164).where(
                PhoneNumber.provider == carrier,
                PhoneNumber.provisioning_state.notin_(("released", "failed")),
                PhoneNumber.inbound_registered_at.is_not(None),
            )
        )
        return list(rows.scalars().all())


async def _carriers(numbers: list[str], routed_only: bool) -> dict[str, str]:
    if not numbers:
        return {}
    where = [
        PhoneNumber.e164.in_(numbers),
        PhoneNumber.provisioning_state.notin_(("released", "failed")),
    ]
    if routed_only:
        where.append(PhoneNumber.inbound_registered_at.is_not(None))
    async with session_scope() as db:
        rows = await db.execute(
            select(PhoneNumber.e164, PhoneNumber.provider).where(*where)
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
    if back and spec.headers_to_attributes and settings.hail_sip_inbound_secret:
        # The shared trunk does not map the header: once on it, every call to
        # these numbers is refused while Hail checks the secret.
        raise TrunkError(
            "HAIL_SIP_INBOUND_SECRET is set: do the Secret header rollback "
            "first (empty the key in .env, copy .env to the VM, redeploy), "
            "then move --back"
        )
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
                "(--drop-unknown removes them once the carrier trunk lists a "
                "moved number; before that, remove them with lk sip inbound update)"
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
        # The app may have unrouted it since the plan: its unregister would
        # then run before this add and leave it listed on the carrier trunk.
        # Read again right before the move.
        if not back and (await carriers_of([number])).get(number) != spec.carrier:
            plan.append(f"skipped {number}: it is not routed now")
            continue
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
        mapped = t.headers_to_attributes.get(SIP_SECRET_HEADER) == SIP_SECRET_ATTRIBUTE
        lines.append(
            f"{t.name} ({role}): "
            f"{len([n for n in t.numbers if n not in placeholders])} number(s), "
            f"allowed addresses {addresses}, "
            f"digest auth {'on' if t.auth_username else 'off'}, "
            f"secret header mapped {'yes' if mapped else 'no'}, "
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
            return await apply(lk.sip, data, spec, args.yes, rotate=args.rotate)
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
        if name == "apply":
            p.add_argument(
                "--rotate",
                action="store_true",
                help="replace a different secret already in a Twilio origination URL",
            )
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
