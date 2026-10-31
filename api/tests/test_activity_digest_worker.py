import asyncio
import hashlib
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from hailhq.api.activity_digest.queries import Receipt
from hailhq.api.activity_digest.report import ActivityData
from hailhq.api.activity_digest.worker import (
    DigestClient,
    DigestService,
    DigestSettings,
    DigestWorker,
    RetryLater,
    ReviewRequired,
)
from hailhq.api.idempotency import STILL_PROCESSING
from hailhq.core.schemas import EmailCreate

NOW = datetime(2026, 10, 29, 9, tzinfo=timezone.utc)
ORG = str(uuid4())
EMAIL_ID = str(uuid4())


@pytest.fixture
def setup():
    config = DigestSettings(
        enabled=True,
        api_url="http://api:8080/v1/",
        api_key="test-key",
        from_address="reports@mail.hail.so",
        to_address="r@hail.so",
    )
    data = ActivityData(
        organizations=[
            {
                "id": ORG,
                "name": "Private org",
                "emails": ["private@example.com"],
                "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            }
        ],
        traffic=[
            {
                "org_id": ORG,
                "channel": "Calls",
                "direction": "outbound",
                "status": "completed",
                "source": "user",
                "count": 1,
            }
        ],
    )
    repository = AsyncMock()
    repository.snapshot.return_value = (None, data)
    return config, repository


def transport_handler(
    requests, *, send_status=202, sender_status="verified", fail_send=False
):
    def handle(request):
        requests.append(request)
        path = request.url.path.removeprefix("/v1")
        if path == "/whoami":
            return httpx.Response(
                200, json={"organization_id": ORG, "auth_kind": "apikey"}
            )
        if path == "/email-domains":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "kind": "hail_mail",
                            "domain": "reports@mail.hail.so",
                            "verification_status": sender_status,
                        }
                    ],
                    "next_cursor": None,
                },
            )
        if path == "/emails":
            # Use the real public request schema. The wrong message_type
            # must fail here before any test can claim a successful send.
            EmailCreate.model_validate(json.loads(request.content))
            if fail_send:
                raise httpx.ReadTimeout("ambiguous acceptance", request=request)
            return httpx.Response(send_status, json={"id": EMAIL_ID, "status": "sent"})
        raise AssertionError(f"Unexpected URL: {request.url}")

    return handle


async def test_disabled_mode_has_no_network_or_database_access(setup):
    config, repository = setup
    config.enabled = False
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: pytest.fail("network access"))
    ) as http:
        result = await DigestService(
            config, repository, DigestClient(config, http)
        ).run_once(NOW)
    assert result.status == "disabled"
    repository.snapshot.assert_not_awaited()


async def test_preview_reads_data_without_calling_hail(setup):
    config, repository = setup
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: pytest.fail("network access"))
    ) as http:
        result = await DigestService(
            config, repository, DigestClient(config, http)
        ).run_once(NOW, preview=True)
    assert result.status == "preview"
    assert "Calls:\n• Outbound: 1" in result.preview[0]
    assert "private@example.com" not in json.dumps(result.summary())


async def test_public_api_payload_and_key_match_the_existing_report_receipts(setup):
    config, repository = setup
    requests = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport_handler(requests))
    ) as http:
        result = await DigestService(
            config, repository, DigestClient(config, http)
        ).run_once(NOW)
    assert result.status == "submitted"
    assert result.email_id == EMAIL_ID
    send = next(r for r in requests if r.method == "POST")
    body = json.loads(send.content)
    assert str(send.url) == "http://api:8080/v1/emails"
    assert send.headers["authorization"] == "Bearer test-key"
    suffix = hashlib.sha256(b"r@hail.so").hexdigest()[:20]
    assert send.headers["idempotency-key"] == f"daily-activity-v1:2026-10-29:{suffix}"
    assert body["message_type"] == "informational"
    assert body["recipient_consent"] is True
    assert body["to"] == ["r@hail.so"]
    assert body["from"] == "reports@mail.hail.so"
    assert body["metadata"]["hail_report"] == "daily_activity"
    assert body["metadata"]["report_key"] == send.headers["idempotency-key"]
    assert body["body_html"]
    assert body["body_text"]


async def test_restart_uses_the_durable_receipt_and_does_not_send_again(setup):
    config, repository = setup
    repository.snapshot.return_value = (Receipt(EMAIL_ID, "delivered", NOW), None)
    requests = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport_handler(requests))
    ) as http:
        for _ in range(2):
            result = await DigestService(
                config, repository, DigestClient(config, http)
            ).run_once(NOW)
            assert result.status == "already_submitted"
    assert not any(r.method == "POST" for r in requests)
    assert not any(r.url.path.endswith("/email-domains") for r in requests)


async def test_worker_rechecks_a_fresh_queued_receipt_until_it_goes_stale(setup):
    config, repository = setup
    repository.snapshot.return_value = (Receipt(EMAIL_ID, "queued", NOW), None)
    requests = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport_handler(requests))
    ) as http:
        service = DigestService(config, repository, DigestClient(config, http))
        worker = DigestWorker(service, clock=lambda: NOW, poll_interval=0.001)
        task = asyncio.create_task(worker.run_forever())
        async with asyncio.timeout(1):
            while repository.snapshot.await_count < 2:
                await asyncio.sleep(0.001)
        await worker.stop()
        await asyncio.wait_for(task, timeout=1)
    assert worker.handled_date is None
    assert not any(r.method == "POST" for r in requests)


@pytest.mark.parametrize("status", ["failed", "bounced", "complained", "queued"])
async def test_failed_or_stale_receipts_require_review(setup, status):
    config, repository = setup
    repository.snapshot.return_value = (
        Receipt(EMAIL_ID, status, NOW.replace(hour=7)),
        None,
    )
    requests = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport_handler(requests))
    ) as http:
        with pytest.raises(ReviewRequired):
            await DigestService(
                config, repository, DigestClient(config, http)
            ).run_once(NOW)
    assert not any(r.method == "POST" for r in requests)


@pytest.mark.parametrize(
    "recipient",
    [
        "customer@example.com",
        "r@hail.so,customer@example.com",
        "r@haıl.so",
        "r@hail.ſo",
    ],
)
async def test_recipient_is_one_internal_address(setup, recipient):
    config, repository = setup
    config.to_address = recipient
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: pytest.fail("network access"))
    ) as http:
        with pytest.raises(ReviewRequired, match="recipient_invalid"):
            await DigestService(
                config, repository, DigestClient(config, http)
            ).run_once(NOW)
    repository.snapshot.assert_not_awaited()


@pytest.mark.parametrize(
    "sender",
    [
        "",
        "reports@hail",
        "reports hail.so",
        "<reports@hail.so>",
        "a,b@hail.so",
        "reports@hail.so\n",
    ],
)
def test_an_invalid_sender_fails_validation(setup, sender):
    config, _ = setup
    config.from_address = sender
    with pytest.raises(ReviewRequired, match="sender_invalid"):
        config.validate_send()


@pytest.mark.parametrize("status", [301, 308, 401, 403, 404])
async def test_a_rejected_read_requires_review(setup, status):
    config, repository = setup
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(status, json={}))
    ) as http:
        with pytest.raises(ReviewRequired, match=f"api_rejection_{status}"):
            await DigestService(
                config, repository, DigestClient(config, http)
            ).run_once(NOW)


@pytest.mark.parametrize("status", [302, 307, 408, 429, 503])
async def test_a_busy_api_retries_with_its_status_code(setup, status):
    config, repository = setup
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(status, json={}))
    ) as http:
        with pytest.raises(RetryLater, match=f"api_status_{status}"):
            await DigestService(
                config, repository, DigestClient(config, http)
            ).run_once(NOW)


async def test_a_send_still_in_progress_retries(setup):
    config, _ = setup
    answer = httpx.Response(409, json={"detail": STILL_PROCESSING})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: answer)
    ) as http:
        with pytest.raises(RetryLater, match="api_status_409"):
            await DigestClient(config, http).request("emails", payload={}, key="k")


async def test_a_reused_key_with_a_new_body_requires_review(setup):
    config, _ = setup
    answer = httpx.Response(409, json={"detail": "reused with a different body"})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: answer)
    ) as http:
        with pytest.raises(ReviewRequired, match="api_rejection_409"):
            await DigestClient(config, http).request("emails", payload={}, key="k")


async def test_unverified_sender_never_sends(setup):
    config, repository = setup
    requests = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            transport_handler(requests, sender_status="pending")
        )
    ) as http:
        with pytest.raises(ReviewRequired, match="sender"):
            await DigestService(
                config, repository, DigestClient(config, http)
            ).run_once(NOW)
    assert not any(r.method == "POST" for r in requests)


async def test_ambiguous_send_is_not_retried_inside_the_run(setup):
    config, repository = setup
    requests = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport_handler(requests, fail_send=True))
    ) as http:
        with pytest.raises(httpx.ReadTimeout):
            await DigestService(
                config, repository, DigestClient(config, http)
            ).run_once(NOW)
    assert sum(r.method == "POST" for r in requests) == 1


async def test_cached_rejections_require_review_without_changing_keys(setup):
    config, repository = setup
    requests = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport_handler(requests, send_status=402))
    ) as http:
        with pytest.raises(ReviewRequired, match="402"):
            await DigestService(
                config, repository, DigestClient(config, http)
            ).run_once(NOW)
    assert sum(r.method == "POST" for r in requests) == 1


async def test_database_failure_never_sends_partial_data(setup):
    config, repository = setup
    repository.snapshot.side_effect = RuntimeError("private@example.com")
    requests = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport_handler(requests))
    ) as http:
        with pytest.raises(RuntimeError):
            await DigestService(
                config, repository, DigestClient(config, http)
            ).run_once(NOW)
    assert not any(r.method == "POST" for r in requests)


async def wait_for_runs(service, count=1):
    async with asyncio.timeout(1):
        while service.run_once.await_count < count:
            await asyncio.sleep(0)


async def test_worker_handles_a_day_once_and_stops_promptly(caplog):
    service = AsyncMock()
    from hailhq.api.activity_digest.worker import Result

    service.run_once.return_value = Result("submitted", "2026-10-29", EMAIL_ID)
    worker = DigestWorker(service, clock=lambda: NOW, poll_interval=0.001)
    task = asyncio.create_task(worker.run_forever())
    await wait_for_runs(service)
    await asyncio.sleep(0.01)
    await worker.stop()
    await asyncio.wait_for(task, timeout=1)
    service.run_once.assert_awaited_once()
    assert "private@example.com" not in caplog.text


async def test_worker_waits_until_the_next_day_after_a_terminal_rejection(caplog):
    service = AsyncMock()
    service.run_once.side_effect = ReviewRequired("api_rejection_402")
    worker = DigestWorker(service, clock=lambda: NOW, poll_interval=0.001)
    task = asyncio.create_task(worker.run_forever())
    await wait_for_runs(service)
    await asyncio.sleep(0.01)
    await worker.stop()
    await asyncio.wait_for(task, timeout=1)
    service.run_once.assert_awaited_once()
    assert "api_rejection_402" in caplog.text


async def test_worker_logs_the_retry_code_and_keeps_the_date_open(caplog):
    service = AsyncMock()
    service.run_once.side_effect = RetryLater("api_status_503")
    worker = DigestWorker(service, clock=lambda: NOW, poll_interval=0.001)
    task = asyncio.create_task(worker.run_forever())
    await wait_for_runs(service, 2)
    await worker.stop()
    await asyncio.wait_for(task, timeout=1)
    assert "failed code=api_status_503" in caplog.text


async def test_worker_does_not_log_exception_content(caplog):
    service = AsyncMock()
    service.run_once.side_effect = RuntimeError("private@example.com secret-bearer")
    worker = DigestWorker(service, clock=lambda: NOW, poll_interval=0.001)
    task = asyncio.create_task(worker.run_forever())
    await wait_for_runs(service)
    await worker.stop()
    await asyncio.wait_for(task, timeout=1)
    assert "failed type=RuntimeError" in caplog.text
    assert "private@example.com" not in caplog.text
    assert "secret-bearer" not in caplog.text
