"""Tests for scripts/ops/sip_invite_probe.py against a local fake SIP server."""

from __future__ import annotations

import importlib.util
import socket
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "sip_invite_probe", HERE / "sip_invite_probe.py"
)
probe = importlib.util.module_from_spec(spec)
sys.modules["sip_invite_probe"] = probe
spec.loader.exec_module(probe)

NUMBER = "+14155550100"


def _response(request: bytes, code: int, reason: str, method="INVITE") -> bytes:
    head = request.split(b"\r\n\r\n", 1)[0].decode()
    keep = [
        line
        for line in head.split("\r\n")[1:]
        if line.split(":", 1)[0] in ("Via", "From", "Call-ID")
    ]
    to = next(line for line in head.split("\r\n") if line.startswith("To:"))
    lines = [
        f"SIP/2.0 {code} {reason}",
        *keep,
        to + (";tag=srv" if code > 100 else ""),
        f"CSeq: 1 {method}",
        "Contact: <sip:room@127.0.0.1:5060;transport=tcp>",
        "Content-Length: 0",
    ]
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def serve(script):
    """One-connection server. ``script(conn, invite)`` answers; every
    message the probe sends is recorded."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    seen: list[bytes] = []

    def run():
        conn, _ = srv.accept()
        with conn:
            conn.settimeout(2)
            invite = conn.recv(65536)
            seen.append(invite)
            script(conn, invite)
            try:
                while chunk := conn.recv(65536):
                    seen.append(chunk)
            except OSError:
                pass
        srv.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return srv.getsockname()[1], seen, thread


def test_final_refusal_passes() -> None:
    port, seen, thread = serve(
        lambda conn, inv: conn.sendall(
            _response(inv, 100, "Trying") + _response(inv, 403, "Forbidden")
        )
    )
    code, verdict = probe.probe("127.0.0.1", port, NUMBER, timeout=2)
    thread.join(3)
    assert code == 0
    assert verdict == "REFUSED: 403 Forbidden"
    assert seen[0].startswith(f"INVITE sip:{NUMBER}@127.0.0.1;transport=tcp".encode())
    assert any(b"ACK " in chunk for chunk in seen[1:])


def test_password_challenge_passes() -> None:
    port, _seen, thread = serve(
        lambda conn, inv: conn.sendall(_response(inv, 407, "Proxy Auth Required"))
    )
    code, verdict = probe.probe("127.0.0.1", port, NUMBER, timeout=2)
    thread.join(3)
    assert code == 0
    assert "407" in verdict


def test_silence_counts_as_refused() -> None:
    port, _seen, thread = serve(lambda conn, inv: None)
    code, verdict = probe.probe("127.0.0.1", port, NUMBER, timeout=0.5)
    thread.join(3)
    assert code == 0
    assert "no answer" in verdict


def test_ringing_fails_and_is_cancelled() -> None:
    def ring(conn, inv):
        conn.sendall(_response(inv, 180, "Ringing"))
        cancel = conn.recv(65536)
        assert cancel.startswith(b"CANCEL ")
        conn.sendall(
            _response(inv, 200, "OK", method="CANCEL")
            + _response(inv, 487, "Request Terminated")
        )

    port, _seen, thread = serve(ring)
    code, verdict = probe.probe("127.0.0.1", port, NUMBER, timeout=2)
    thread.join(3)
    assert code == 1
    assert verdict == "ACCEPTED: 180 Ringing (cancelled)"


def test_answered_call_fails_and_is_hung_up() -> None:
    port, seen, thread = serve(
        lambda conn, inv: conn.sendall(_response(inv, 200, "OK"))
    )
    code, _verdict = probe.probe("127.0.0.1", port, NUMBER, timeout=2)
    thread.join(3)
    assert code == 1
    sent = b"".join(seen[1:])
    assert b"ACK sip:room@127.0.0.1" in sent
    assert b"BYE sip:room@127.0.0.1" in sent


def test_no_connection_says_nothing() -> None:
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()
    code, verdict = probe.probe("127.0.0.1", port, NUMBER, timeout=1)
    assert code == 2
    assert verdict.startswith("NO CONNECTION")
