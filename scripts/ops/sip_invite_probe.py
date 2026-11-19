"""Send one SIP INVITE from this machine and say if the trunk refused it.

    python3 scripts/ops/sip_invite_probe.py \\
        --host <project>.sip.livekit.cloud --number +<E.164 number>

The negative test of docs/operations/inbound-trunk-lockdown.md: run it from
an address that is not a carrier's, without credentials, at a number listed
on a locked trunk. The INVITE goes over TCP to port 5060, from caller
+12025550100 (a fictional number).

Verdicts and exit codes:

* 0 REFUSED: LiveKit's refusal (403, 404 "Does not match any SIP Trunks" or
  "No trunk found", or 401/407 asking for a password this probe does not
  have), or no answer at all (LiveKit drops what it takes for a flood).
  Check that the number is listed on the trunk: a mistyped number gets the
  same 404.
* 1 ACCEPTED: the call rang (18x) or was answered (2xx). The trunk is not
  locked. The probe cancels or hangs up the call at once, but it may reach
  the number's agent and show in its call log for a moment.
* 2 the test says nothing: no connection, a connection that failed, another
  final code (printed), or 100 Trying with no final answer.

Standard library only.
"""

from __future__ import annotations

import argparse
import re
import secrets
import socket
import sys
from dataclasses import dataclass, field

CALLER = "+12025550100"
E164 = re.compile(r"^\+[1-9]\d{6,14}$")
CONNECT_TIMEOUT = 10.0
ANSWER_TIMEOUT = 15.0
# What LiveKit answers a call no trunk accepts (livekit/sip, pkg/sip/inbound.go):
# 404 "Does not match any SIP Trunks" or "No trunk found", 403 "Auth failure",
# and 401/407 to ask for a password.
REFUSED_CODES = frozenset({401, 403, 404, 407})


@dataclass
class Response:
    code: int
    reason: str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Dialog:
    host: str
    port: int
    number: str
    local_ip: str
    local_port: int
    call_id: str = field(default_factory=lambda: secrets.token_hex(12))
    from_tag: str = field(default_factory=lambda: secrets.token_hex(6))
    branch: str = field(default_factory=lambda: "z9hG4bK" + secrets.token_hex(8))

    @property
    def uri(self) -> str:
        return f"sip:{self.number}@{self.host};transport=tcp"

    def _via(self, branch: str | None = None) -> str:
        return (
            f"SIP/2.0/TCP {self.local_ip}:{self.local_port}"
            f";branch={branch or self.branch};rport"
        )

    def _common(self, method: str, cseq: int, to_tag: str = "", branch=None) -> list:
        to = f"<sip:{self.number}@{self.host}>" + (f";tag={to_tag}" if to_tag else "")
        return [
            f"Via: {self._via(branch)}",
            "Max-Forwards: 70",
            f"From: <sip:{CALLER}@{self.local_ip}>;tag={self.from_tag}",
            f"To: {to}",
            f"Call-ID: {self.call_id}",
            f"CSeq: {cseq} {method}",
            "User-Agent: hail-sip-probe",
        ]

    def invite(self) -> bytes:
        sdp = (
            "v=0\r\n"
            f"o=- 0 0 IN IP4 {self.local_ip}\r\n"
            "s=hail-probe\r\n"
            f"c=IN IP4 {self.local_ip}\r\n"
            "t=0 0\r\n"
            "m=audio 40000 RTP/AVP 0\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
        )
        lines = [f"INVITE {self.uri} SIP/2.0", *self._common("INVITE", 1)]
        lines += [
            f"Contact: <sip:probe@{self.local_ip}:{self.local_port};transport=tcp>",
            "Content-Type: application/sdp",
            f"Content-Length: {len(sdp.encode())}",
        ]
        return ("\r\n".join(lines) + "\r\n\r\n" + sdp).encode()

    def cancel(self) -> bytes:
        return _bare(f"CANCEL {self.uri} SIP/2.0", self._common("CANCEL", 1))

    def ack(self, to_tag: str, final_ok: bool, target: str | None = None) -> bytes:
        # A 2xx ACK is a new transaction; a non-2xx ACK reuses the INVITE's.
        branch = "z9hG4bK" + secrets.token_hex(8) if final_ok else None
        uri = target or self.uri
        return _bare(f"ACK {uri} SIP/2.0", self._common("ACK", 1, to_tag, branch))

    def bye(self, to_tag: str, target: str | None = None) -> bytes:
        branch = "z9hG4bK" + secrets.token_hex(8)
        uri = target or self.uri
        return _bare(f"BYE {uri} SIP/2.0", self._common("BYE", 2, to_tag, branch))


def _bare(first: str, headers: list[str]) -> bytes:
    return ("\r\n".join([first, *headers, "Content-Length: 0"]) + "\r\n\r\n").encode()


class Reader:
    """Splits SIP messages out of a TCP stream (Content-Length framing)."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buf = b""

    def next(self) -> Response | None:
        while True:
            head_end = self.buf.find(b"\r\n\r\n")
            if head_end >= 0:
                head = self.buf[:head_end].decode("utf-8", "replace")
                headers = _headers(head)
                length = int(headers.get("content-length", "0") or 0)
                total = head_end + 4 + length
                if len(self.buf) >= total:
                    self.buf = self.buf[total:]
                    first = head.split("\r\n", 1)[0]
                    if not first.startswith("SIP/2.0 "):
                        continue  # a request from the server: not ours to answer
                    _, code, *reason = first.split(" ", 2)
                    return Response(int(code), " ".join(reason), headers)
            try:
                chunk = self.sock.recv(65536)
            except TimeoutError:
                return None
            if not chunk:
                raise ConnectionError("closed by the server")
            self.buf += chunk


def _headers(head: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in head.split("\r\n")[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            out.setdefault(key.strip().lower(), value.strip())
    return out


def _tag(to_header: str) -> str:
    match = re.search(r";\s*tag=([^;>\s]+)", to_header)
    return match.group(1) if match else ""


def _contact(headers: dict[str, str]) -> str | None:
    match = re.search(r"<([^>]+)>", headers.get("contact", ""))
    return match.group(1) if match else None


def probe(host: str, port: int, number: str, timeout: float = ANSWER_TIMEOUT):
    """Returns (exit code, verdict line)."""
    try:
        sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
    except OSError as exc:
        return 2, f"NO CONNECTION to {host}:{port} ({exc}): the test says nothing"
    with sock:
        sock.settimeout(timeout)
        local_ip, local_port = sock.getsockname()[:2]
        dialog = Dialog(host, port, number, local_ip, local_port)
        state = {"accepted": None, "answered": False}
        try:
            return _dialog(sock, dialog, state)
        except OSError as exc:
            accepted = state["accepted"]
            if accepted:
                return 1, f"ACCEPTED: {accepted.code} {accepted.reason}"
            return 2, f"INCONCLUSIVE: the connection failed ({exc})"


def _dialog(sock: socket.socket, dialog: Dialog, state: dict) -> tuple[int, str]:
    sock.sendall(dialog.invite())
    reader = Reader(sock)
    while True:
        response = reader.next()
        accepted = state["accepted"]
        if response is None:
            if accepted:
                return 1, f"ACCEPTED: {accepted.code} {accepted.reason}"
            if state["answered"]:
                return 2, "INCONCLUSIVE: 100 Trying, then no final answer"
            # LiveKit drops what it takes for a flood without an answer.
            return 0, "REFUSED: no answer to the INVITE (dropped)"
        if "INVITE" not in response.headers.get("cseq", ""):
            continue  # the answer to our CANCEL
        state["answered"] = True
        if response.code < 180:
            continue
        to_tag = _tag(response.headers.get("to", ""))
        if response.code < 200:
            if accepted is None:
                state["accepted"] = response
                sock.sendall(dialog.cancel())
            continue
        if response.code < 300:
            target = _contact(response.headers)
            sock.sendall(dialog.ack(to_tag, True, target))
            sock.sendall(dialog.bye(to_tag, target))
            return 1, f"ACCEPTED: {response.code} {response.reason} (hung up)"
        sock.sendall(dialog.ack(to_tag, False))
        if accepted:
            return 1, f"ACCEPTED: {accepted.code} {accepted.reason} (cancelled)"
        if response.code in REFUSED_CODES:
            return 0, f"REFUSED: {response.code} {response.reason}"
        return 2, f"INCONCLUSIVE: {response.code} {response.reason}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", required=True, help="<project>.sip.livekit.cloud")
    parser.add_argument("--number", required=True, help="E.164 number on the trunk")
    parser.add_argument("--port", type=int, default=5060)
    args = parser.parse_args(argv)
    if not E164.match(args.number):
        parser.error("--number must be E.164, like +14155550100")
    code, verdict = probe(args.host, args.port, args.number)
    print(verdict)
    return code


if __name__ == "__main__":
    sys.exit(main())
