"""Tests for scripts/ops/place_test_call.py. HTTP is mocked: no real calls."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "place_test_call", HERE / "place_test_call.py"
)
ptc = importlib.util.module_from_spec(spec)
sys.modules["place_test_call"] = ptc
spec.loader.exec_module(ptc)

API = "https://api.example.test/"
CALL_ID = "11111111-1111-1111-1111-111111111111"


def call(status, **fields):
    return {"id": CALL_ID, "status": status, "end_reason": None, **fields}


class FakeTime:
    """A clock that only moves when the code sleeps."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def client_for(responses, seen=None):
    """A client whose transport answers with ``responses`` in order. An
    item that is an exception is raised instead."""
    queue = list(responses)

    def handler(request):
        if seen is not None:
            seen.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.Client(transport=httpx.MockTransport(handler))


def wait(client, timer, **kw):
    return ptc.wait_for_end(
        client, API, CALL_ID, sleep=timer.sleep, clock=timer.clock, **kw
    )


def test_polls_until_terminal_status():
    seen = []
    timer = FakeTime()
    client = client_for(
        [
            httpx.Response(200, json=call("dialing")),
            httpx.Response(200, json=call("in_progress")),
            httpx.Response(200, json=call("completed", end_reason="normal_hangup")),
        ],
        seen,
    )
    result = wait(client, timer, interval=5)
    assert result["status"] == "completed"
    assert timer.sleeps == [5, 5]
    assert all(str(r.url) == f"{API}v1/calls/{CALL_ID}" for r in seen)


def test_rate_limit_and_server_errors_are_retried():
    timer = FakeTime()
    client = client_for(
        [
            httpx.Response(429, headers={"Retry-After": "12"}),
            httpx.Response(503),
            httpx.ConnectError("reset"),
            httpx.Response(200, text="<html>bad gateway</html>"),
            httpx.Response(200, json=call("busy", end_reason="user_rejected")),
        ]
    )
    result = wait(client, timer, interval=5)
    assert result["status"] == "busy"
    assert timer.sleeps == [12, 5, 5, 5]


def test_client_error_stops_the_wait():
    client = client_for([httpx.Response(404, json={"detail": "not found"})])
    with pytest.raises(ptc.CallTestError, match="404"):
        wait(client, FakeTime())


def test_timeout_names_the_call():
    timer = FakeTime()
    client = client_for([httpx.Response(200, json=call("ringing"))] * 10)
    with pytest.raises(ptc.CallTestError, match=CALL_ID) as exc:
        wait(client, timer, timeout=20, interval=5)
    assert timer.now == 20
    assert "last status ringing" in str(exc.value)


def test_timeout_names_a_body_that_is_not_a_call():
    timer = FakeTime()
    client = client_for([httpx.Response(200, text="<html>portal</html>")] * 10)
    with pytest.raises(ptc.CallTestError, match="not a call"):
        wait(client, timer, timeout=10, interval=5)


def test_long_retry_after_still_polls_at_the_deadline():
    timer = FakeTime()
    client = client_for(
        [
            httpx.Response(429, headers={"Retry-After": "60"}),
            httpx.Response(200, json=call("completed", end_reason="normal_hangup")),
        ]
    )
    result = wait(client, timer, timeout=50, interval=5)
    assert result["status"] == "completed"
    assert timer.sleeps == [50]


def test_unexpected_body_on_place_says_the_call_may_be_live(monkeypatch, capsys):
    monkeypatch.setenv("HAIL_API_KEY", "hl_test")
    monkeypatch.setattr(ptc, "place_call", lambda *a: {"unexpected": True})
    code = ptc.main(["--number", "+14155550100", "--i-will-call-a-customer"])
    assert code == 2
    assert "may have been placed" in capsys.readouterr().err


def test_place_call_sends_the_documented_body():
    seen = []
    client = client_for([httpx.Response(201, json=call("queued"))], seen)
    placed = ptc.place_call(client, API, "+14155550100", None)
    assert placed["id"] == CALL_ID
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == f"{API}v1/calls"
    assert request.headers["Idempotency-Key"]
    body = json.loads(request.content)
    assert body["to"] == "+14155550100"
    assert body["recipient_consent"] is True
    assert body["tools"] == ["end_call"]
    assert "from" not in body


def test_place_call_refusal_is_an_error():
    client = client_for([httpx.Response(402, text="insufficient credits")])
    with pytest.raises(ptc.CallTestError, match="402"):
        ptc.place_call(client, API, "+14155550100", "+14155550199")


def test_verdict_answered_with_duration():
    answered, text = ptc.verdict(
        call(
            "completed",
            end_reason="normal_hangup",
            answered_at="2026-11-17T10:00:03Z",
            ended_at="2026-11-17T10:00:45Z",
        )
    )
    assert answered
    assert text.startswith("ANSWERED")
    assert "42 s" in text
    assert "end_reason=normal_hangup" in text


def test_verdict_not_answered_names_the_reason():
    answered, text = ptc.verdict(
        call(
            "failed",
            end_reason="sip_trunk_failure",
            failure_message="The carrier refused the call.",
        )
    )
    assert not answered
    assert text.startswith("NOT ANSWERED")
    assert "end_reason=sip_trunk_failure" in text
    assert "The carrier refused the call." in text


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--i-will-call-a-customer"],
        ["--number", "+14155550100"],
        ["--number", "14155550100", "--i-will-call-a-customer"],
    ],
)
def test_refuses_without_number_and_confirmation(argv, monkeypatch):
    monkeypatch.setenv("HAIL_API_KEY", "hl_test")
    with pytest.raises(SystemExit) as exc:
        ptc.main(argv)
    assert exc.value.code == 2


def test_refuses_without_api_key(monkeypatch, capsys):
    monkeypatch.delenv("HAIL_API_KEY", raising=False)
    code = ptc.main(["--number", "+14155550100", "--i-will-call-a-customer"])
    assert code == 2
    assert "HAIL_API_KEY" in capsys.readouterr().err


def test_verdict_completed_without_answered_at_counts_as_answered():
    answered, text = ptc.verdict(call("completed", end_reason="normal_hangup"))
    assert answered
    assert text.startswith("ANSWERED")


def test_network_error_on_place_says_the_call_may_be_live(monkeypatch, capsys):
    monkeypatch.setenv("HAIL_API_KEY", "hl_test")

    def boom(*args, **kwargs):
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(ptc, "place_call", boom)
    code = ptc.main(["--number", "+14155550100", "--i-will-call-a-customer"])
    assert code == 2
    assert "may have been placed" in capsys.readouterr().err


def test_timeout_prints_the_call_id(monkeypatch, capsys):
    monkeypatch.setenv("HAIL_API_KEY", "hl_test")
    monkeypatch.setattr(ptc, "place_call", lambda *a: call("queued"))

    def late(*args, **kwargs):
        raise ptc.CallTestError("did not end")

    monkeypatch.setattr(ptc, "wait_for_end", late)
    code = ptc.main(["--number", "+14155550100", "--i-will-call-a-customer"])
    assert code == 2
    assert CALL_ID in capsys.readouterr().err


def test_null_call_id_says_the_call_may_be_live(monkeypatch, capsys):
    monkeypatch.setenv("HAIL_API_KEY", "hl_test")
    monkeypatch.setattr(ptc, "place_call", lambda *a: {"id": None})
    code = ptc.main(["--number", "+14155550100", "--i-will-call-a-customer"])
    assert code == 2
    assert "may have been placed" in capsys.readouterr().err


def test_place_call_body_that_is_not_json_raises_value_error():
    # main() turns a ValueError from place_call into the call-may-be-live
    # warning, so place_call must not swallow it.
    client = client_for([httpx.Response(201, text="<html>ok</html>")])
    with pytest.raises(ValueError):
        ptc.place_call(client, API, "+14155550100", None)


def test_refuses_an_api_url_without_scheme(monkeypatch, capsys):
    monkeypatch.setenv("HAIL_API_KEY", "hl_test")
    monkeypatch.setenv("HAIL_API_URL", "api.example.test")
    code = ptc.main(["--number", "+14155550100", "--i-will-call-a-customer"])
    assert code == 2
    assert "HAIL_API_URL" in capsys.readouterr().err


def test_a_null_status_is_not_a_call():
    timer = FakeTime()
    body = {"id": CALL_ID, "status": None}
    client = client_for([httpx.Response(200, json=body)] * 10)
    with pytest.raises(ptc.CallTestError, match="not a call"):
        wait(client, timer, timeout=10, interval=5)


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.test",
        "http://localhost.evil",
        "http://localhost:pw@evil.example.test/",
        "ftp://localhost",
    ],
)
def test_refuses_plain_http_to_another_host(url, monkeypatch, capsys):
    monkeypatch.setenv("HAIL_API_KEY", "hl_test")
    monkeypatch.setenv("HAIL_API_URL", url)
    code = ptc.main(["--number", "+14155550100", "--i-will-call-a-customer"])
    assert code == 2
    assert "HAIL_API_URL" in capsys.readouterr().err


@pytest.mark.parametrize(
    "url",
    [
        "https://api.hail.so",
        "HTTPS://api.hail.so",
        "http://localhost:8080",
        "http://127.0.0.1/",
        "http://[::1]:8080",
    ],
)
def test_accepts_https_and_local_http(url):
    assert ptc.safe_api_url(url)


STILL = {"detail": "request with this idempotency key is still processing"}


def post(client, timer=None):
    timer = timer or FakeTime()
    return ptc.place_call(
        client, API, "+14155550100", None, sleep=timer.sleep, clock=timer.clock
    )


def test_place_call_retries_with_one_key_until_the_call_comes_back():
    seen = []
    timer = FakeTime()
    client = client_for(
        [
            httpx.ReadTimeout("slow"),
            httpx.Response(429, headers={"Retry-After": "5"}),
            httpx.Response(409, json=STILL),
            httpx.Response(201, json=call("queued")),
        ],
        seen,
    )
    assert post(client, timer)["id"] == CALL_ID
    assert timer.sleeps == [2, 5, 2]
    assert len({r.headers["Idempotency-Key"] for r in seen}) == 1


def test_place_call_unknown_outcome_may_be_live():
    timer = FakeTime()
    client = client_for([httpx.Response(409, json=STILL)] * 100)
    with pytest.raises(ptc.CallMaybeLive, match="409"):
        post(client, timer)
    assert timer.now <= ptc.POST_RETRY_WINDOW


def test_place_call_no_answer_at_all_may_be_live():
    client = client_for([httpx.ReadTimeout("slow")] * 100)
    with pytest.raises(ptc.CallMaybeLive, match="ReadTimeout"):
        post(client)


def test_place_call_final_conflict_is_a_plain_error():
    body = {"detail": "idempotency key reused with a different request body"}
    client = client_for([httpx.Response(409, json=body)])
    with pytest.raises(ptc.CallTestError, match="409"):
        post(client)


def test_place_call_fresh_server_error_may_be_live():
    client = client_for([httpx.Response(502, text="dispatch failed")] * 100)
    with pytest.raises(ptc.CallMaybeLive, match="502"):
        post(client)


def test_place_call_replayed_server_error_is_final():
    replay = {"Idempotency-Replay": "true"}
    client = client_for(
        [httpx.ReadTimeout("slow"), httpx.Response(502, headers=replay)]
    )
    with pytest.raises(ptc.CallTestError, match="502"):
        post(client)


def test_place_call_fresh_refusal_after_a_lost_request_retries_until_replay():
    # The lost request claims the key later; its replayed refusal is final.
    replay = {"Idempotency-Replay": "true"}
    seen = []
    client = client_for(
        [
            httpx.ReadTimeout("slow"),
            httpx.Response(402),
            httpx.Response(409, json=STILL),
            httpx.Response(403, headers=replay),
        ],
        seen,
    )
    with pytest.raises(ptc.CallTestError, match="403"):
        post(client)
    assert len(seen) == 4


def test_place_call_fresh_refusal_after_a_lost_request_may_be_live():
    client = client_for([httpx.ReadTimeout("slow")] + [httpx.Response(402)] * 100)
    with pytest.raises(ptc.CallMaybeLive, match="402"):
        post(client)


def test_place_call_refusal_after_a_connect_error_is_final():
    # A connect error never sent the request, so nothing can be live.
    client = client_for([httpx.ConnectError("dns"), httpx.Response(404)])
    with pytest.raises(ptc.CallTestError, match="404"):
        post(client)


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(429),
        httpx.Response(
            503, json={"detail": "shared call line pool exhausted; try again"}
        ),
    ],
)
def test_place_call_retryable_refusal_after_a_timeout_may_be_live(answer):
    client = client_for([httpx.ReadTimeout("slow")] + [answer] * 100)
    with pytest.raises(ptc.CallMaybeLive):
        post(client)


def test_place_call_last_try_lands_at_the_deadline():
    timer = FakeTime()
    client = client_for(
        [
            httpx.Response(429, headers={"Retry-After": "200"}),
            httpx.Response(201, json=call("queued")),
        ]
    )
    assert post(client, timer)["id"] == CALL_ID
    assert timer.sleeps == [ptc.POST_RETRY_WINDOW]


def test_place_call_waits_out_a_fresh_rate_limit():
    timer = FakeTime()
    client = client_for(
        [
            httpx.Response(429, headers={"Retry-After": "5"}),
            httpx.Response(201, json=call("queued")),
        ]
    )
    assert post(client, timer)["id"] == CALL_ID
    assert timer.sleeps == [5]


def test_place_call_rate_limit_that_never_clears_is_final():
    client = client_for([httpx.Response(429)] * 100)
    with pytest.raises(ptc.CallTestError, match="429"):
        post(client)


def test_place_call_replayed_rate_limit_is_final():
    replay = {"Idempotency-Replay": "true"}
    client = client_for(
        [httpx.ReadTimeout("slow"), httpx.Response(429, headers=replay)]
    )
    with pytest.raises(ptc.CallTestError, match="429"):
        post(client)


POOL = {"detail": "shared call line pool exhausted; try again shortly"}


def test_place_call_waits_for_a_free_shared_line_with_one_key():
    seen = []
    timer = FakeTime()
    client = client_for(
        [httpx.Response(503, json=POOL), httpx.Response(201, json=call("queued"))],
        seen,
    )
    assert post(client, timer)["id"] == CALL_ID
    assert timer.sleeps == [2]
    assert len({r.headers["Idempotency-Key"] for r in seen}) == 1


def test_place_call_shared_lines_that_stay_busy_are_final():
    client = client_for([httpx.Response(503, json=POOL)] * 100)
    with pytest.raises(ptc.CallTestError, match="503"):
        post(client)


def test_place_call_other_503_after_a_timeout_may_be_live():
    client = client_for(
        [httpx.ReadTimeout("slow")] + [httpx.Response(503, text="no upstream")] * 100
    )
    with pytest.raises(ptc.CallMaybeLive, match="503"):
        post(client)


def test_place_call_slow_first_try_still_gets_a_retry():
    timer = FakeTime()
    answers = [httpx.ReadTimeout("slow"), httpx.Response(201, json=call("queued"))]

    def handler(request):
        item = answers.pop(0)
        if isinstance(item, Exception):
            timer.now += 30  # the try used the whole client timeout
            raise item
        return item

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert post(client, timer)["id"] == CALL_ID


def test_retry_after_ignores_bad_values():
    for value in ["inf", "nan", "-3", "Wed, 21 Oct 2026 07:28:00 GMT"]:
        assert (
            ptc._retry_after(httpx.Response(429, headers={"Retry-After": value})) == 0
        )


@pytest.mark.parametrize("timeout", ["0", "-5", "nan", "inf"])
def test_refuses_a_bad_timeout(timeout, monkeypatch):
    monkeypatch.setenv("HAIL_API_KEY", "hl_test")
    argv = [
        "--number",
        "+14155550100",
        "--i-will-call-a-customer",
        "--timeout",
        timeout,
    ]
    with pytest.raises(SystemExit) as exc:
        ptc.main(argv)
    assert exc.value.code == 2


def test_place_call_never_sent_is_final():
    client = client_for([httpx.ConnectError("dns")] * 100)
    with pytest.raises(ptc.CallTestError, match="never sent"):
        post(client)


def test_place_call_gateway_error_is_retried_until_replay():
    replay = {"Idempotency-Replay": "true"}
    client = client_for(
        [httpx.Response(504), httpx.Response(201, json=call("queued"), headers=replay)]
    )
    assert post(client)["id"] == CALL_ID


def test_place_call_in_flight_request_then_no_answer_may_be_live():
    seen = []
    client = client_for(
        [httpx.ReadTimeout("slow"), httpx.Response(409, json=STILL)]
        + [httpx.ConnectError("dns")] * 100,
        seen,
    )
    with pytest.raises(ptc.CallMaybeLive):
        post(client)
    assert len({r.headers["Idempotency-Key"] for r in seen}) == 1


def test_place_call_pool_timeout_was_never_sent():
    client = client_for([httpx.PoolTimeout("busy")] * 100)
    with pytest.raises(ptc.CallTestError, match="never sent"):
        post(client)


def test_place_call_lost_after_a_5xx_survives_later_connect_errors():
    seen = []
    client = client_for([httpx.Response(502)] + [httpx.ConnectError("dns")] * 100, seen)
    with pytest.raises(ptc.CallMaybeLive):
        post(client)
    assert len({r.headers["Idempotency-Key"] for r in seen}) == 1
