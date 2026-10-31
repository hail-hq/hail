"""Run the opt-in Compose worker, or prepare one private local preview."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
from pathlib import Path

import httpx
from hailhq.core.db import engine_options, to_async_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from .queries import Repository
from .worker import (
    DigestClient,
    DigestService,
    DigestSettings,
    DigestWorker,
    RetryLater,
    ReviewRequired,
)

logger = logging.getLogger(__name__)


def write_preview(directory: Path, result):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, body in zip(("report.txt", "report.html"), result.preview, strict=True):
        fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(body)


async def execute(args):
    config = DigestSettings()
    one_shot = args.once or args.preview
    stop = asyncio.Event()
    if not one_shot:
        # Only the long-running worker checks this event. A one-shot run
        # keeps the default handlers, so Ctrl-C and SIGTERM stop it.
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
    if not config.enabled and not args.preview:
        print(json.dumps({"status": "disabled"}), flush=True)
        if not one_shot:
            await stop.wait()
        return 0
    if not config.database_url.get_secret_value():
        raise ReviewRequired("database_url_missing")
    if not args.preview:
        config.validate_send()
    database_url = config.database_url.get_secret_value()
    # A pooled connection would sit idle all day and hold a database slot.
    engine = create_async_engine(
        to_async_url(database_url),
        poolclass=NullPool,
        **engine_options(database_url),
    )
    try:
        async with httpx.AsyncClient(follow_redirects=False) as http:
            service = DigestService(
                config, Repository(engine), DigestClient(config, http)
            )
            worker = DigestWorker(service, stop_event=stop)
            if one_shot:
                async with asyncio.timeout(120):
                    result = await service.run_once(
                        worker.clock(), preview=args.preview
                    )
                if result.preview and args.output_dir:
                    write_preview(args.output_dir, result)
                print(json.dumps(result.summary()), flush=True)
            else:
                await worker.run_forever()
    finally:
        await engine.dispose()
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--once",
        action="store_true",
        help="One due check. Sending still requires the enabled flag.",
    )
    mode.add_argument(
        "--preview",
        action="store_true",
        help="Read-only report. Never calls the Hail API.",
    )
    parser.add_argument(
        "--output-dir", type=Path, help="Private preview files. Requires --preview."
    )
    args = parser.parse_args()
    if args.output_dir and not args.preview:
        parser.error("--output-dir requires --preview")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    try:
        return asyncio.run(execute(args))
    except ReviewRequired as error:
        logger.error("activity_digest requires_review code=%s", error)
        return 1
    except RetryLater as error:
        logger.error("activity_digest command_failed code=%s", error)
        return 1
    except Exception as error:
        # Never print exception reprs with SQL parameters, URLs, or headers.
        logger.error("activity_digest command_failed type=%s", type(error).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
