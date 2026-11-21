"""Mark and unmark the pool numbers that send US trial texts.

Run from the repo root of a checkout whose ``.env`` holds the production
values (``Settings`` reads ``.env`` from the working directory):

    uv run python scripts/ops/sms_trial_numbers.py status
    uv run python scripts/ops/sms_trial_numbers.py mark --number +E164
    uv run python scripts/ops/sms_trial_numbers.py unmark --number +E164
    uv run python scripts/ops/sms_trial_numbers.py move --number +E164
    uv run python scripts/ops/sms_trial_numbers.py pause --number +E164
    uv run python scripts/ops/sms_trial_numbers.py resume --number +E164

Every command except ``status`` prints its plan and changes nothing until
``--yes`` is added.

* ``mark``: a shared call pool number becomes a trial texting number. It
  then never serves calls or browser sessions.
* ``unmark``: a trial texting number goes back to the shared pool (calls
  only when it can make calls).
* ``move``: a workspace's number leaves the workspace and becomes a trial
  texting pool number.
* ``pause`` / ``resume``: stop trial texts from a number at once, and start
  them again. A paused number stays a trial number.

``mark`` and ``move`` take an active US toll-free number that can text, on
a carrier that confirms toll-free approval (Twilio). After
``mark`` or ``move`` with ``--yes``, the script asks the carrier once
whether the number's toll-free verification is approved, so the number is
ready at once (the verification worker repeats this every pass). Code:
core/hailhq/core/sms_trial_pool.py. Runbook:
docs/operations/sms-trial-numbers.md.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from hailhq.core import sms_trial_pool
from hailhq.core.db import dispose_engine, session_scope
from hailhq.core.sms_trial import refresh_trial_approvals, trial_number_by_e164

ANSWERS = {
    "approved": "approved.",
    "not_approved": "not approved. Get it approved at the carrier first.",
    "no_answer": "no answer (lookup failed). The next verification pass asks again.",
}
CHANGES = {
    "mark": sms_trial_pool.mark,
    "unmark": sms_trial_pool.unmark,
    "move": sms_trial_pool.move,
    "pause": sms_trial_pool.pause,
    "resume": sms_trial_pool.resume,
}


async def run(args: argparse.Namespace) -> list[str]:
    lines: list[str] = []
    try:
        async with session_scope() as db:
            if args.command != "status":
                lines += await CHANGES[args.command](db, args.number, apply=args.yes)
                if not args.yes:
                    await db.rollback()
                    return lines
                await db.commit()
                if args.command in {"mark", "move"}:
                    number = await trial_number_by_e164(db, args.number)
                    if number is not None:
                        counts = await refresh_trial_approvals(
                            db, number_ids=[number.id]
                        )
                        answer = next(k for k, n in counts.items() if n)
                        lines.append(f"Carrier toll-free approval: {ANSWERS[answer]}")
            lines += await sms_trial_pool.status(db)
        return lines
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    for name in CHANGES:
        p = sub.add_parser(name)
        p.add_argument("--number", required=True, metavar="+E164")
        p.add_argument("--yes", action="store_true", help="make the change")
    args = parser.parse_args(argv)
    try:
        lines = asyncio.run(run(args))
    except sms_trial_pool.TrialPoolError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print("\n".join(lines))
    if args.command != "status" and not args.yes:
        print("Dry run: nothing changed. Add --yes to make this change.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
