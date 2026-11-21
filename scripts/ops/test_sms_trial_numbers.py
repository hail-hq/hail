"""Tests for scripts/ops/sms_trial_numbers.py (the database changes are
tested in core/tests/test_sms_trial_pool.py)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from hailhq.core.sms_trial_pool import TrialPoolError

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "sms_trial_numbers", HERE / "sms_trial_numbers.py"
)
stn = importlib.util.module_from_spec(spec)
sys.modules["sms_trial_numbers"] = stn
spec.loader.exec_module(stn)


def test_changes_are_a_dry_run_without_yes(monkeypatch, capsys):
    seen = []

    async def fake_run(args):
        seen.append((args.command, args.number, args.yes))
        return ["mark +18005550199: leaves the call pool and sends only trial texts."]

    monkeypatch.setattr(stn, "run", fake_run)
    assert stn.main(["mark", "--number", "+18005550199"]) == 0
    assert seen == [("mark", "+18005550199", False)]
    assert "Dry run: nothing changed" in capsys.readouterr().out


def test_yes_makes_the_change(monkeypatch, capsys):
    seen = []

    async def fake_run(args):
        seen.append(args.yes)
        return ["done"]

    monkeypatch.setattr(stn, "run", fake_run)
    assert stn.main(["move", "--number", "+18005550199", "--yes"]) == 0
    assert seen == [True]
    assert "Dry run" not in capsys.readouterr().out


def test_a_refused_change_exits_2(monkeypatch, capsys):
    async def fake_run(args):
        raise TrialPoolError("+18005550199: not a trial texting number.")

    monkeypatch.setattr(stn, "run", fake_run)
    assert stn.main(["unmark", "--number", "+18005550199"]) == 2
    assert "ERROR: +18005550199: not a trial texting number." in capsys.readouterr().err


def test_pause_and_resume_are_commands(monkeypatch):
    seen = []

    async def fake_run(args):
        seen.append(args.command)
        return []

    monkeypatch.setattr(stn, "run", fake_run)
    for command in ("pause", "resume"):
        assert stn.main([command, "--number", "+18005550199", "--yes"]) == 0
    assert seen == ["pause", "resume"]
    assert stn.CHANGES["pause"] is stn.sms_trial_pool.pause
