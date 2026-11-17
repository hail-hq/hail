"""Place one test call to a customer's number and say if it was answered.

    export HAIL_API_KEY='<API key from your own Hail workspace>'
    uv run python scripts/ops/place_test_call.py \\
        --number +<E.164 number> --i-will-call-a-customer

The call goes out through Hail's public API from the workspace the key
belongs to (``POST /v1/calls``), then the script polls
``GET /v1/calls/{id}`` until the call ends. It prints one verdict line:
ANSWERED (with how long the call lasted) or NOT ANSWERED, with the call's
status and end reason. Exit code: 0 answered, 1 not answered, 2 the call
could not be placed or did not end in time.

The call reaches the customer's agent and shows in their call log. Remove
it afterwards: docs/operations/call-record-deletion.md. When to run this:
docs/operations/first-number-call-test.md.

``HAIL_API_URL`` overrides the API address (default https://api.hail.so).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from urllib.parse import urlsplit

import httpx
from hailhq.core.schemas import TERMINAL_CALL_STATUSES
from hailhq.core.urls import join_url

DEFAULT_API_URL = "https://api.hail.so"
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
E164 = re.compile(r"^\+[1-9]\d{6,14}$")
# Voice calls stop at 300 seconds by default; this leaves room for ringing.
DEFAULT_TIMEOUT_SECONDS = 420
POLL_SECONDS = 5.0
SYSTEM_PROMPT = (
    "This is a short line test from Hail, the phone provider. Say hello, "
    "listen to the answer, thank them, then use end_call. Keep the call "
    "under one minute."
)
FIRST_MESSAGE = "Hello, this is a short line test from Hail."
MAYBE_LIVE = (
    "ERROR: no clear answer from POST /v1/calls ({exc}). The call may have "
    "been placed: check your workspace's call log before you run this again."
)
WARNING = (
    "This call reaches the customer's agent and shows in their call log.\n"
    "Remove it afterwards: docs/operations/call-record-deletion.md"
)


class CallTestError(Exception):
    """The call could not be placed, or did not end in time."""


class CallMaybeLive(Exception):
    """POST /v1/calls gave no clear answer: the call may be live."""


def call_body(number: str, from_number: str | None) -> dict:
    body = {
        "to": number,
        "recipient_consent": True,
        "message_type": "informational",
        "system_prompt": SYSTEM_PROMPT,
        "first_message": FIRST_MESSAGE,
        "tools": ["end_call"],
        "recording_enabled": False,
        "metadata": {"purpose": "line_test"},
    }
    if from_number:
        body["from"] = from_number
    return body


def place_call(
    client: httpx.Client,
    api_url: str,
    number: str,
    from_number: str | None,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    url = join_url(api_url, "v1/calls")
    body = call_body(number, from_number)
    headers = {"Idempotency-Key": str(uuid.uuid4())}
    try:
        response = client.post(url, json=body, headers=headers)
    except httpx.TransportError:
        # Same key: if Hail took the first request, it answers with that
        # call instead of placing a second one.
        sleep(2)
        response = client.post(url, json=body, headers=headers)
        if response.status_code != 201:
            # A 409 means the first request is still being handled.
            raise CallMaybeLive(
                f"retry answered {response.status_code}: {response.text}"
            )
    if response.status_code != 201:
        raise CallTestError(
            f"POST /v1/calls answered {response.status_code}: {response.text}"
        )
    return response.json()


def wait_for_end(
    client: httpx.Client,
    api_url: str,
    call_id: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    interval: float = POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    """Poll until the call has a terminal status. A rate limit, a server
    error, a network error or a body that is not a call waits and tries
    again; only the timeout or a client error (4xx other than 429) stops
    the wait."""
    url = join_url(api_url, f"v1/calls/{call_id}")
    deadline = clock() + timeout
    last = "no answer yet"
    while True:
        wait = interval
        try:
            response = client.get(url)
        except httpx.HTTPError as exc:
            response = None
            last = f"last poll failed: {exc!r}"
        if response is not None:
            if response.status_code == 200:
                call = _json(response)
                if not isinstance(call, dict) or not isinstance(
                    call.get("status"), str
                ):
                    last = f"last poll body is not a call: {response.text[:200]!r}"
                elif call["status"] in TERMINAL_CALL_STATUSES:
                    return call
                else:
                    last = f"last status {call['status']}"
            elif response.status_code == 429:
                wait = max(interval, _retry_after(response))
                last = "last poll rate limited"
            elif response.status_code < 500:
                raise CallTestError(
                    f"GET /v1/calls/{call_id} answered "
                    f"{response.status_code}: {response.text}"
                )
            else:
                last = f"last poll answered {response.status_code}"
        left = deadline - clock()
        if left <= 0:
            raise CallTestError(
                f"call {call_id} did not end within {timeout:.0f} seconds ({last})"
            )
        # The last wait is cut short so one more poll lands at the deadline.
        sleep(min(wait, left))


def _json(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:
        return None


def _retry_after(response: httpx.Response) -> float:
    try:
        return float(response.headers.get("Retry-After", "0"))
    except ValueError:
        return 0.0


def _seconds(start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    return round(
        (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    )


def verdict(call: dict) -> tuple[bool, str]:
    """True when someone picked up: ``answered_at`` is set, or the call
    completed (the voicebot's billing rule; ``answered_at`` can be missed).
    The text names the status and the end reason either way, and the
    failure message when there is one."""
    answered = call.get("answered_at") is not None or call.get("status") == "completed"
    detail = f"status={call.get('status')} end_reason={call.get('end_reason')}"
    if answered:
        seconds = _seconds(call.get("answered_at"), call.get("ended_at"))
        lasted = f" after {seconds} s" if seconds is not None else ""
        return True, f"ANSWERED: the call was picked up, ended{lasted}. {detail}"
    text = f"NOT ANSWERED: {detail}"
    if call.get("failure_message"):
        text += f". {call['failure_message']}"
    return False, text


def safe_api_url(url: str) -> bool:
    """The API key goes in a header: https, or plain http to this machine."""
    parts = urlsplit(url)
    if parts.scheme == "https":
        return bool(parts.hostname)
    return parts.scheme == "http" and parts.hostname in LOCAL_HOSTS


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Place one test call to a number through Hail's public API.",
        epilog=WARNING,
    )
    parser.add_argument(
        "--number", required=True, help="number to call, E.164 (+<digits>)"
    )
    parser.add_argument(
        "--i-will-call-a-customer",
        dest="confirmed",
        action="store_true",
        help="required: confirms you know the call reaches the customer's agent",
    )
    parser.add_argument(
        "--from",
        dest="from_number",
        help="caller ID, a number in your own workspace (default: Hail picks one)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"seconds to wait for the call to end (default {DEFAULT_TIMEOUT_SECONDS})",
    )
    args = parser.parse_args(argv)
    if not E164.match(args.number):
        parser.error(f"--number must be E.164, like +14155550100: {args.number!r}")
    if not args.confirmed:
        parser.error(
            "add --i-will-call-a-customer to place the call.\n" + WARNING,
        )
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    api_key = os.environ.get("HAIL_API_KEY")
    if not api_key:
        print(
            "Set HAIL_API_KEY in this shell to a key from your own workspace.",
            file=sys.stderr,
        )
        return 2
    api_url = os.environ.get("HAIL_API_URL") or DEFAULT_API_URL
    if not safe_api_url(api_url):
        print(
            f"HAIL_API_URL must start with https:// (http:// only for "
            f"localhost): {api_url!r}",
            file=sys.stderr,
        )
        return 2
    print(WARNING)
    headers = {"Authorization": f"Bearer {api_key}"}
    with httpx.Client(headers=headers, timeout=30) as client:
        try:
            placed = place_call(client, api_url, args.number, args.from_number)
        except CallTestError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        except (CallMaybeLive, httpx.HTTPError, ValueError) as exc:
            # The request may have reached Hail: the call can be live now.
            print(MAYBE_LIVE.format(exc=repr(exc)), file=sys.stderr)
            return 2
        call_id = placed.get("id") if isinstance(placed, dict) else None
        if not isinstance(call_id, str) or not call_id:
            print(MAYBE_LIVE.format(exc=f"body {placed!r}"), file=sys.stderr)
            return 2
        print(f"Placed call {call_id} to {args.number}.")
        print(f"Caller ID used: {placed.get('from_e164')}. Waiting for the end.")
        try:
            call = wait_for_end(client, api_url, call_id, timeout=args.timeout)
        except CallTestError as exc:
            print(f"ERROR: {exc}. Call id: {call_id}", file=sys.stderr)
            return 2
    answered, text = verdict(call)
    print(text)
    print(f"Call id (your workspace): {call_id}")
    if answered:
        # A carrier announcement or voicemail also picks up. Only the
        # customer's inbound row shows the call reached their agent.
        print(
            "Confirm the customer's inbound call row: "
            "docs/operations/first-number-call-test.md#read-the-result"
        )
    return 0 if answered else 1


if __name__ == "__main__":
    sys.exit(main())
