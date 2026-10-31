import asyncio
import json
import stat

from hailhq.api.activity_digest import __main__ as command
from hailhq.api.activity_digest.worker import Result, ReviewRequired


def test_disabled_one_shot_does_not_require_database_or_network(monkeypatch, capsys):
    monkeypatch.setenv("HAIL_ACTIVITY_DIGEST_ENABLED", "false")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr("sys.argv", ["activity-digest", "--once"])
    monkeypatch.setattr(
        command,
        "create_async_engine",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("database access")),
    )
    assert command.main() == 0
    assert json.loads(capsys.readouterr().out) == {"status": "disabled"}


def test_invalid_config_logs_the_error_kind_without_the_secret(monkeypatch, caplog):
    monkeypatch.setenv("HAIL_ACTIVITY_DIGEST_ENABLED", "true")
    monkeypatch.setenv("HAIL_ACTIVITY_DIGEST_API_KEY", "do-not-log-this-key")
    monkeypatch.setenv("HAIL_ACTIVITY_DIGEST_API_URL", "not-a-url")
    monkeypatch.setattr("sys.argv", ["activity-digest", "--once"])
    assert command.main() == 1
    assert "requires_review code=invalid_settings:api_url" in caplog.text
    assert "do-not-log-this-key" not in caplog.text
    assert "not-a-url" not in caplog.text


def test_one_shot_keeps_default_signal_handling(monkeypatch):
    handlers = []
    monkeypatch.setenv("HAIL_ACTIVITY_DIGEST_ENABLED", "false")
    monkeypatch.setattr("sys.argv", ["activity-digest", "--once"])
    monkeypatch.setattr(
        asyncio.SelectorEventLoop,
        "add_signal_handler",
        lambda self, *args: handlers.append(args),
    )
    assert command.main() == 0
    assert handlers == []


def test_one_shot_logs_the_review_code(monkeypatch, caplog):
    async def needs_review(args):
        raise ReviewRequired("sender_not_verified")

    monkeypatch.setattr("sys.argv", ["activity-digest", "--once"])
    monkeypatch.setattr(command, "execute", needs_review)
    assert command.main() == 1
    assert "requires_review code=sender_not_verified" in caplog.text


def test_missing_settings_log_a_stable_code(monkeypatch, caplog):
    monkeypatch.setenv("HAIL_ACTIVITY_DIGEST_ENABLED", "true")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr("sys.argv", ["activity-digest", "--once"])
    assert command.main() == 1
    assert "requires_review code=database_url_missing" in caplog.text


def test_digest_engine_uses_the_url_options(monkeypatch):
    seen = {}

    def capture(url, **options):
        seen.update(options)
        raise RuntimeError("stop before connecting")

    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@h/db?pgbouncer=true")
    monkeypatch.setattr("sys.argv", ["activity-digest", "--preview"])
    monkeypatch.setattr(command, "create_async_engine", capture)
    assert command.main() == 1
    assert seen == {
        "poolclass": command.NullPool,
        "connect_args": {
            "statement_cache_size": 0,
            "prepared_statement_cache_size": 0,
        },
    }


def test_private_preview_files_are_not_readable_by_other_users(tmp_path):
    for name in ("report.txt", "report.html"):
        path = tmp_path / name
        path.write_text("old")
        path.chmod(0o644)
    command.write_preview(
        tmp_path,
        Result(
            "preview", "2026-10-29", preview=("private text", "<p>private text</p>")
        ),
    )
    assert (tmp_path / "report.txt").read_text() == "private text"
    assert (tmp_path / "report.html").read_text() == "<p>private text</p>"
    assert stat.S_IMODE((tmp_path / "report.txt").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "report.html").stat().st_mode) == 0o600
