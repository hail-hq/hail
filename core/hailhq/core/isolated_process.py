"""Runs a native file decoder in a child process with no credentials.

Untrusted files (email attachments, verification documents) are decoded by
``python -m <module>`` in a child process: its environment holds only
``PATH``, the module sets its own CPU and memory limits, and the child is
killed when the time is up or the caller stops waiting. A crash, a hang or
a memory bug in a native decoder never reaches the service process or its
credentials. Used by the voicebot's attachment reader and the verification
document reader.
"""

from __future__ import annotations

import asyncio
import os
import sys

__all__ = ["run_isolated"]


async def run_isolated(
    module: str,
    *args: str,
    stdin: bytes,
    timeout_s: float,
    max_output: int,
) -> bytes:
    """The child's stdout. Raises ``TimeoutError`` past ``timeout_s``, and
    ``ValueError`` when the child fails, writes nothing, or writes more than
    ``max_output`` bytes. The child is killed on any way out."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        module,
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONUTF8": "1"},
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(stdin), timeout_s)
        if process.returncode or not stdout or len(stdout) > max_output:
            raise ValueError("the file could not be decoded")
        return stdout
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
