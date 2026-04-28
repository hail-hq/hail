"""Voicebot worker entrypoint.

Run with::

    cd voicebot && uv run python -m hailhq.voicebot.main start

``cli.run_app`` parses the ``start`` / ``dev`` / ``download-files``
subcommands itself; we just hand it the :class:`WorkerOptions`. The worker
binds to **explicit dispatch** via ``agent_name="hail-voicebot"`` — the API
service's ``LiveKitClient.dispatch_agent`` matches on this name.
"""

from __future__ import annotations

from livekit.agents import WorkerOptions, cli

from hailhq.voicebot.agent import entrypoint, prewarm


def main() -> None:
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            agent_name="hail-voicebot",
        )
    )


if __name__ == "__main__":
    main()
