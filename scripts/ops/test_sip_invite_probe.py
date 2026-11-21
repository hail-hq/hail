"""Tests for scripts/ops/sip_invite_probe.py against a local fake SIP server."""

from __future__ import annotations

import importlib.util
import socket
import struct
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
            if conn.fileno() < 0:
                srv.close()
                return
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


def test_silence_says_nothing() -> None:
    """LiveKit also drops INVITEs it takes for a flood: no answer is not a
    refusal."""
    port, _seen, thread = serve(lambda conn, inv: None)
    code, verdict = probe.probe("127.0.0.1", port, NUMBER, timeout=0.5)
    thread.join(3)
    assert code == 2
    assert verdict.startswith("INCONCLUSIVE: no answer")
    assert "wait a minute" in verdict


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


def test_other_final_codes_say_nothing() -> None:
    """486 can be Hail refusing a call LiveKit accepted: not proof of a lock."""
    port, _seen, thread = serve(
        lambda conn, inv: conn.sendall(_response(inv, 486, "Busy Here"))
    )
    code, verdict = probe.probe("127.0.0.1", port, NUMBER, timeout=2)
    thread.join(3)
    assert code == 2
    assert verdict == "INCONCLUSIVE: 486 Busy Here"


def test_trying_then_silence_says_nothing() -> None:
    port, seen, thread = serve(
        lambda conn, inv: conn.sendall(_response(inv, 100, "Trying"))
    )
    code, verdict = probe.probe("127.0.0.1", port, NUMBER, timeout=0.5)
    thread.join(3)
    assert code == 2
    assert "100 Trying" in verdict
    assert b"CANCEL " in b"".join(seen[1:])


def test_reset_connection_says_nothing() -> None:
    def reset(conn, _inv):
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        conn.close()

    port, _seen, thread = serve(reset)
    code, verdict = probe.probe("127.0.0.1", port, NUMBER, timeout=2)
    thread.join(3)
    assert code == 2
    assert verdict.startswith("INCONCLUSIVE")


def test_closed_connection_says_nothing() -> None:
    port, _seen, thread = serve(lambda conn, _inv: conn.close())
    code, verdict = probe.probe("127.0.0.1", port, NUMBER, timeout=2)
    thread.join(3)
    assert code == 2
    assert "closed by the server" in verdict


def test_answer_then_reset_is_still_accepted() -> None:
    """A trunk that answered is unlocked, even if the hang-up cannot be sent."""

    def answer_and_reset(conn, inv):
        conn.sendall(_response(inv, 200, "OK"))
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        conn.close()

    port, _seen, thread = serve(answer_and_reset)
    code, verdict = probe.probe("127.0.0.1", port, NUMBER, timeout=2)
    thread.join(3)
    assert code == 1
    assert verdict.startswith("ACCEPTED: 200")
