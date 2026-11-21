"""The verification worker's pass refreshes the trial numbers' cached
carrier approval, in its own step."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from hailhq.api import verification_worker as vw


class _Session:
    def __init__(self):
        self.rollbacks = 0

    async def rollback(self):
        self.rollbacks += 1


@pytest.fixture
def worker(monkeypatch):
    session = _Session()

    @asynccontextmanager
    async def factory():
        yield session

    async def close(*_args):
        return None

    async def sweep(_session, _registry):
        return {"submitted": 0}

    async def livekit():
        return None

    monkeypatch.setattr(vw, "close_org_carrier_items", close)
    monkeypatch.setattr(vw, "sweep_verifications", sweep)
    monkeypatch.setattr(vw, "get_livekit_optional", livekit)
    return SimpleNamespace(
        worker=vw.VerificationWorker(session_factory=factory), session=session
    )


async def test_tick_refreshes_trial_approvals(worker, monkeypatch):
    calls = []

    async def refresh(session):
        calls.append(session)
        return {"approved": 1, "not_approved": 0, "no_answer": 2}

    monkeypatch.setattr(vw, "refresh_trial_approvals", refresh)
    counts = await worker.worker.tick()
    assert calls == [worker.session]
    assert counts == {"submitted": 0, "trial_not_approved": 0, "trial_no_answer": 2}


async def test_a_failed_trial_refresh_keeps_the_pass(worker, monkeypatch):
    async def refresh(_session):
        raise RuntimeError("carrier down")

    monkeypatch.setattr(vw, "refresh_trial_approvals", refresh)
    assert await worker.worker.tick() == {"submitted": 0}
    assert worker.session.rollbacks == 1
