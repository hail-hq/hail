"""Bounded email-worker metrics. Message content and identity never become labels."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from time import monotonic

from opentelemetry import metrics
from opentelemetry.trace import StatusCode


class ReplyObservation:
    def __init__(self, span):
        self.span = span
        self.outcome = "failed"

    def finish(self, outcome: str) -> None:
        self.outcome = (
            outcome
            if outcome
            in {"done", "skipped", "failed", "retry", "superseded", "cancelled"}
            else "failed"
        )
        if self.span is not None:
            self.span.set_attribute("reply.state", self.outcome)
            if self.outcome == "failed":
                self.span.set_status(StatusCode.ERROR)

    def error(self, exc: BaseException) -> None:
        if self.span is not None:
            self.span.record_exception(exc)
            self.span.set_status(StatusCode.ERROR)


@contextmanager
def observe_reply(requested_at: datetime, span=None):
    """Count one claimed attempt, its duration, and its age since receipt."""
    meter = metrics.get_meter("hailhq.email")
    attempts = meter.create_counter("hail.email.reply.attempts", unit="{attempt}")
    duration = meter.create_histogram("hail.email.reply.duration", unit="s")
    age = meter.create_histogram("hail.email.reply.queue_age", unit="s")
    if requested_at.tzinfo is None:
        requested_at = requested_at.replace(tzinfo=timezone.utc)
    age.record(max(0.0, (datetime.now(timezone.utc) - requested_at).total_seconds()))
    started = monotonic()
    report = ReplyObservation(span)
    try:
        yield report
    except BaseException as exc:
        report.error(exc)
        report.finish("cancelled" if not isinstance(exc, Exception) else "failed")
        raise
    finally:
        report.finish(report.outcome)
        labels = {"outcome": report.outcome}
        attempts.add(1, labels)
        duration.record(max(0.0, monotonic() - started), labels)
