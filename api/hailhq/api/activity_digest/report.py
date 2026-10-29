"""Daily cutoffs and the owner's activity email. No network or database writes."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

from hailhq.core.urls import join_url

ZONE = ZoneInfo("Europe/Stockholm")
REPORT_KIND = "daily_activity"
CREATION_ACTIONS = [
    "call.create",
    "sms.create",
    "email.create",
    "agent.sms.reply",
    "agent.sms.send",
    "agent.email.send",
    "agent.handover.sms",
]
SOURCES = {
    "incoming": "incoming traffic",
    "forward": "automatic forwarding",
    "system": "automation",
    "api_key": "API key",
    "user": "user token",
    "superadmin": "staff user token",
    "unknown": "source not recorded",
}


@dataclass(frozen=True)
class ReportWindow:
    date: str
    start: datetime
    end: datetime


@dataclass
class ActivityData:
    organizations: list[dict[str, Any]] = field(default_factory=list)
    traffic: list[dict[str, Any]] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    accesses: list[dict[str, Any]] = field(default_factory=list)


def daily_window(now: datetime) -> ReportWindow | None:
    if now.tzinfo is None:
        raise ValueError("The report clock must include a timezone")
    local = now.astimezone(ZONE)
    end = datetime.combine(local.date(), time(8), tzinfo=ZONE)
    if local < end:
        return None
    start = datetime.combine(local.date() - timedelta(days=1), time(8), tzinfo=ZONE)
    return ReportWindow(
        local.date().isoformat(),
        start.astimezone(timezone.utc),
        end.astimezone(timezone.utc),
    )


def next_due(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("The report clock must include a timezone")
    local = now.astimezone(ZONE)
    due = datetime.combine(local.date(), time(8), tzinfo=ZONE)
    if local >= due:
        due = datetime.combine(local.date() + timedelta(days=1), time(8), tzinfo=ZONE)
    return due.astimezone(timezone.utc)


def display_time(value: datetime) -> str:
    return value.astimezone(ZONE).strftime("%d %b %Y, %H:%M")


def one_line(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def format_report(data: ActivityData, window: ReportWindow) -> str:
    sections = []
    for org in sorted(data.organizations, key=lambda org: org["name"].casefold()):
        org_id = org["id"]
        traffic = [r for r in data.traffic if r["org_id"] == org_id and r["count"] > 0]
        actions = [r for r in data.actions if r["org_id"] == org_id and r["count"] > 0]
        accesses = [r for r in data.accesses if r["org_id"] == org_id]
        created = window.start <= org["created_at"] < window.end
        if not (traffic or actions or accesses or created):
            continue
        lines = [
            one_line(org["name"]),
            ", ".join(one_line(email) for email in org["emails"])
            or "Owner email unavailable",
        ]
        for channel in ("Calls", "SMS", "Emails"):
            rows = [r for r in traffic if r["channel"] == channel]
            if not rows:
                continue
            lines.extend(["", f"{channel}:"])
            for direction in ("inbound", "outbound"):
                subset = [r for r in rows if r["direction"] == direction]
                if not subset:
                    continue
                statuses: Counter[str] = Counter()
                origins: Counter[str] = Counter()
                for row in subset:
                    statuses[row["status"]] += row["count"]
                    origins[SOURCES.get(row["source"], SOURCES["unknown"])] += row[
                        "count"
                    ]
                status_text = ", ".join(
                    f"{key}: {n}" for key, n in sorted(statuses.items())
                )
                source_text = ", ".join(
                    f"{key}: {n}" for key, n in sorted(origins.items())
                )
                lines.append(
                    f"• {direction.title()}: {sum(statuses.values())} ({status_text}). Sources: {source_text}."
                )
        if actions:
            lines.extend(["", "Setup and other actions:"])
            for row in actions:
                lines.append(
                    f"• {one_line(row['action'])}: {row['count']}. Source: {SOURCES.get(row['source'], SOURCES['unknown'])}."
                )
        if accesses:
            latest = max(r["last_at"] for r in accesses)
            lines.extend(
                [
                    "",
                    "API access:",
                    f"• Latest API-key request: {display_time(latest)}.",
                ]
            )
        if created:
            lines.extend(["", "Account:", "• Workspace created."])
        sections.append("\n".join(lines))
    return "\n\n".join(
        [
            f"Hail daily activity · {window.date}",
            f"{display_time(window.start)} to {display_time(window.end)} (Europe/Stockholm).",
            "\n\n────────────────────\n\n".join(sections)
            or "No activity recorded in this window.",
            "Sources reflect recorded authentication. Console, MCP, and direct API use are not distinguished by the audit log.",
            "API access shows the latest observed API-key request, not a count of requests.",
        ]
    )


def render_html(report: str, window: ReportWindow) -> str:
    # Explicit line breaks work in email clients that ignore white-space CSS.
    body = escape(report).replace("\n", "<br>")
    logo = join_url("https://hail.so", "assets/brand/logo/hail-messenger.png")
    console = join_url("https://hail.so", "console")
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hail daily activity · {window.date}</title></head>
<body style="margin:0;background:#f5f3ed;color:#171916;font-family:Helvetica,Arial,sans-serif;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center" style="padding:24px 0;">
<table role="presentation" width="600" cellspacing="0" cellpadding="0" style="width:100%;max-width:600px;background:#fffef9;border:1px solid #d1d2c9;">
<tr><td style="background:#171916;padding:24px 28px;color:#fffef9;font-size:22px;font-weight:bold;">
<img src="{logo}" alt="" width="32" height="32" style="vertical-align:middle;margin-right:12px;"> hail.so</td></tr>
<tr><td style="padding:28px;overflow-wrap:anywhere;word-break:break-word;">
<div style="font-family:Courier New,monospace;font-size:12px;color:#62635d;">{window.date} · Europe/Stockholm</div>
<h1 style="font-family:Space Grotesk,Helvetica,Arial,sans-serif;font-size:30px;line-height:1.2;margin:10px 0 24px;">Daily activity.</h1>
<div style="font-size:16px;line-height:1.65;">{body}</div>
<p style="margin:24px 0 0;"><a href="{console}" style="display:inline-block;background:#171916;color:#fffef9;padding:14px 19px;text-decoration:none;font-size:14px;font-weight:bold;">Open the console</a></p>
</td></tr></table>
<p style="font-size:12px;color:#62635d;"><a href="mailto:hi@hail.so" style="color:#62635d;">hi@hail.so</a> · Hailing frequencies open.</p>
</td></tr></table></body></html>"""
