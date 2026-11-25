"""Operator command for connected numbers (support and ops; customers use
the console or /v1/sip-connections).

Run on the API host:
``docker compose exec api python -m hailhq.api.connected_numbers_ops <command>``.
The inbound password is printed once, to stdout only. It is never logged.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from uuid import UUID

import phonenumbers
from hailhq.api.connected_numbers import (
    ConnectedNumberError,
    add_connected_number,
    check_connected_number,
    create_connection,
    delete_connection,
    give_way_unchecked_claim,
    remove_connected_number,
)
from hailhq.core.db import session_scope
from hailhq.core.livekit import LiveKitClient
from hailhq.core.models import PhoneNumber, SipConnection

_REGION = re.compile(r"[A-Z]{2}")


def _region(value: str) -> str:
    if not _REGION.fullmatch(value):
        raise argparse.ArgumentTypeError("use a 2-letter country code, like US")
    return value


def _e164(value: str) -> str:
    try:
        parsed = phonenumbers.parse(value, None)
    except phonenumbers.NumberParseException as exc:
        raise argparse.ArgumentTypeError("not a phone number; use +E.164") from exc
    if not phonenumbers.is_possible_number(parsed):
        raise argparse.ArgumentTypeError("not a phone number; use +E.164")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("not a UUID") from exc


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # exit 2 with usage, as argparse does
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="connected_numbers_ops")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="create a connection and its trunks")
    create.add_argument("--org", required=True, type=_uuid)
    create.add_argument("--label", required=True)
    create.add_argument("--region", required=True, type=_region)
    create.add_argument("--pbx-host")
    create.add_argument("--pbx-port", type=int, default=5060)
    create.add_argument(
        "--pbx-transport", choices=("auto", "udp", "tcp", "tls"), default="auto"
    )

    add = sub.add_parser("add", help="add a number to a connection")
    add.add_argument("--connection", required=True, type=_uuid)
    add.add_argument("--number", required=True)

    check = sub.add_parser("check", help="run the ownership check for a number")
    check.add_argument("--number-id", required=True, type=_uuid)

    remove = sub.add_parser("remove", help="remove a number")
    remove.add_argument("--number-id", required=True, type=_uuid)

    delete = sub.add_parser("delete", help="delete a connection with no numbers")
    delete.add_argument("--connection", required=True, type=_uuid)

    release = sub.add_parser(
        "release-claim",
        help="free a number held by an unchecked claim (before adding it by hand)",
    )
    release.add_argument("--number", required=True, type=_e164)

    dispatch = sub.add_parser(
        "dispatch-any", help="make a dispatch rule match any inbound trunk"
    )
    dispatch.add_argument("--rule-name", required=True)
    return parser


async def _connection(db, connection_id: UUID) -> SipConnection:
    row = await db.get(SipConnection, connection_id)
    if row is None:
        raise ConnectedNumberError("not_found", "connection", "no such connection")
    return row


async def _number(db, number_id: UUID) -> PhoneNumber:
    row = await db.get(PhoneNumber, number_id)
    if row is None or row.provider != "pbx":
        raise ConnectedNumberError("not_found", "number_id", "no such number")
    return row


async def _run(args: argparse.Namespace) -> None:
    lk = LiveKitClient()
    try:
        async with session_scope() as db:
            if args.command == "dispatch-any":
                found = await lk.clear_dispatch_rule_trunks(args.rule_name)
                if found is None:
                    raise ConnectedNumberError(
                        "not_found", "rule_name", "no such dispatch rule"
                    )
                rule_id, trunks = found
                print(f"rule id: {rule_id}")
                print(f"trunks: {', '.join(trunks) or 'none'}")
            elif args.command == "release-claim":
                e164 = args.number
                if not await give_way_unchecked_claim(db, lk, e164):
                    print("not an unchecked claim: nothing released")
                    raise SystemExit(1)
                print("released")
            elif args.command == "create":
                row, password = await create_connection(
                    db,
                    lk,
                    organization_id=args.org,
                    label=args.label,
                    default_region=args.region,
                    pbx_host=args.pbx_host,
                    pbx_port=args.pbx_port,
                    pbx_transport=args.pbx_transport,
                )
                print(f"connection id: {row.id}")
                print(f"inbound username: {row.inbound_username}")
                print(f"inbound password: {password}")
            elif args.command == "add":
                connection = await _connection(db, args.connection)
                number = await add_connected_number(db, lk, connection, args.number)
                print(f"{number.id} {number.provisioning_state}")
            elif args.command == "check":
                number = await _number(db, args.number_id)
                result = await check_connected_number(db, lk, number)
                print(f"{result.state} {result.reason or 'none'} {result.call_id}")
            elif args.command == "remove":
                number = await _number(db, args.number_id)
                await remove_connected_number(db, lk, number)
                print("removed")
            else:
                connection = await _connection(db, args.connection)
                await delete_connection(db, lk, connection)
                print("deleted")
    finally:
        await lk.aclose()


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        asyncio.run(_run(args))
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    except ConnectedNumberError as exc:
        print(f"error: {exc.code} field: {exc.field or 'none'}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
