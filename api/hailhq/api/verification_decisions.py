"""Ping the website once a verification is approved or rejected, so it
emails the org's owners (``notify_verification_decided``).

Every path that decides a verification (carrier poll, a draft the carrier
refuses, superadmin reject, a bought number's refused registration) sets
``state`` on the ORM row. These session listeners collect such rows at
flush and ping after the commit, so no path has to remember the call, and
a change that is rolled back sends nothing. A Core ``update()`` of the
state column would bypass them; none exists."""

from __future__ import annotations

from hailhq.core.internal_webhook import notify_verification_decided
from hailhq.core.models import CarrierVerification
from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

_DECIDED = ("approved", "rejected")
_KEY = "verifications_decided"


@event.listens_for(Session, "after_flush")
def _collect(session: Session, _flush_context) -> None:
    for obj in (*session.new, *session.dirty):
        if (
            isinstance(obj, CarrierVerification)
            and obj.state in _DECIDED
            and inspect(obj).attrs.state.history.added
        ):
            session.info.setdefault(_KEY, set()).add(obj.id)


@event.listens_for(Session, "after_commit")
def _ping(session: Session) -> None:
    for verification_id in session.info.pop(_KEY, ()):
        notify_verification_decided(str(verification_id))


@event.listens_for(Session, "after_rollback")
def _drop(session: Session) -> None:
    session.info.pop(_KEY, None)
