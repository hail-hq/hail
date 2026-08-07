"""Voicebot worker entrypoint.

Run with::

    cd voicebot && uv run python -m hailhq.voicebot.main start

``cli.run_app`` parses the ``start`` / ``dev`` / ``download-files``
subcommands itself; we just hand it the :class:`WorkerOptions`. The worker
binds to **explicit dispatch** via ``agent_name="hail-voicebot"`` — the API
service's ``LiveKitClient.dispatch_agent`` matches on this name.
"""

from __future__ import annotations

import logging
import sys

from hailhq.voicebot.agent import entrypoint, prewarm
from hailhq.voicebot.pipeline import startup_capability_warnings
from livekit.agents import WorkerOptions, cli

logger = logging.getLogger("hailhq.voicebot")


def main() -> None:
    # Log startup capability warnings before running the app, but skip for
    # download-files (which runs in Docker builder stage with no secrets).
    if sys.argv[1:2] != ["download-files"]:
        for warning in startup_capability_warnings():
            logger.warning(warning)

    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            agent_name="hail-voicebot",
        )
    )


if __name__ == "__main__":
    main()
