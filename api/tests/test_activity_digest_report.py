from datetime import datetime

import pytest
from hailhq.api.activity_digest.report import (
    ActivityData,
    daily_window,
    format_report,
    render_html,
)


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@pytest.mark.parametrize(
    ("now", "start", "end"),
    [
        ("2026-10-29T09:00Z", "2026-10-28T07:00Z", "2026-10-29T07:00Z"),
        ("2026-07-29T06:00Z", "2026-07-28T06:00Z", "2026-07-29T06:00Z"),
        ("2026-03-29T08:00Z", "2026-03-28T07:00Z", "2026-03-29T06:00Z"),
        ("2026-10-25T10:00Z", "2026-10-24T06:00Z", "2026-10-25T07:00Z"),
    ],
)
def test_window_uses_successive_stockholm_cutoffs(now, start, end):
    window = daily_window(instant(now))
    assert window.start == instant(start)
    assert window.end == instant(end)
    assert window.date == end[:10]


def test_before_schedule_and_naive_clock():
    assert daily_window(instant("2026-10-29T06:59:59Z")) is None
    with pytest.raises(ValueError, match="timezone"):
        daily_window(datetime(2026, 10, 29, 8))  # noqa: DTZ001 - deliberate naive input


@pytest.fixture
def data():
    return ActivityData(
        organizations=[
            {
                "id": "a",
                "name": "Active org",
                "emails": ["owner@example.com"],
                "created_at": instant("2026-01-01T00:00Z"),
            },
            {
                "id": "b",
                "name": "Quiet org",
                "emails": ["quiet@example.com"],
                "created_at": instant("2026-01-01T00:00Z"),
            },
        ],
        traffic=[
            {
                "org_id": "a",
                "channel": "Calls",
                "direction": "inbound",
                "status": "completed",
                "source": "incoming",
                "count": 2,
            },
            {
                "org_id": "a",
                "channel": "SMS",
                "direction": "outbound",
                "status": "delivered",
                "source": "api_key",
                "count": 3,
            },
            {
                "org_id": "a",
                "channel": "Emails",
                "direction": "outbound",
                "status": "delivered",
                "source": "forward",
                "count": 1,
            },
        ],
        actions=[
            {"org_id": "a", "action": "agent.create", "source": "user", "count": 1}
        ],
    )


def test_report_groups_directions_and_omits_empty_categories(data):
    window = daily_window(instant("2026-10-29T09:00Z"))
    report = format_report(data, window)
    assert "Active org\nowner@example.com" in report
    assert "Calls:\n• Inbound: 2" in report
    assert "SMS:\n• Outbound: 3" in report
    assert "automatic forwarding: 1" in report
    assert "API key: 3" in report
    assert "agent.create: 1" in report
    assert "Console, MCP, and direct API use are not distinguished" in report
    assert "Quiet org" not in report
    assert "Calls:\n• Outbound" not in report


def test_report_combines_outcomes_and_keeps_observed_api_access_separate(data):
    data.traffic.append(
        {
            "org_id": "a",
            "channel": "SMS",
            "direction": "outbound",
            "status": "failed",
            "source": "api_key",
            "count": 1,
        }
    )
    data.accesses.append({"org_id": "a", "last_at": instant("2026-10-28T12:39Z")})
    report = format_report(data, daily_window(instant("2026-10-29T09:00Z")))
    assert "SMS:\n• Outbound: 4 (delivered: 3, failed: 1)" in report
    assert "Latest API-key request: 28 Oct 2026, 13:39" in report


def test_signup_only_and_empty_days(data):
    window = daily_window(instant("2026-10-29T09:00Z"))
    data.traffic = []
    data.actions = []
    data.organizations[1]["created_at"] = instant("2026-10-28T09:00Z")
    report = format_report(data, window)
    assert "Quiet org" in report
    assert "Workspace created" in report
    assert "Active org" not in report
    assert "Calls:" not in report
    assert "No activity recorded" in format_report(ActivityData(), window)


def test_html_escapes_account_values_and_preserves_lines_for_email_clients(data):
    window = daily_window(instant("2026-10-29T09:00Z"))
    data.organizations[0]["name"] = '<script>alert("bad")</script>\nCalls: fake'
    report = format_report(data, window)
    assert "</script> Calls: fake\nowner@example.com" in report
    html = render_html(report, window)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "Calls:<br>" in html
    assert "hail-messenger.png" in html
    assert "#fffef9" in html


def test_next_due_remains_local_eight_across_the_clock_change():
    from hailhq.api.activity_digest.report import next_due

    assert next_due(instant("2026-03-28T09:00Z")) == instant("2026-03-29T06:00Z")
    assert next_due(instant("2026-10-24T09:00Z")) == instant("2026-10-25T07:00Z")
    assert next_due(instant("2026-10-29T06:00Z")) == instant("2026-10-29T07:00Z")
