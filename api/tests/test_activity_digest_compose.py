"""Exercise the real Compose parser without starting services or reading .env."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def compose(tmp_path, profile, *args, overlay):
    # Older Compose versions still check service env_file paths with
    # --no-env-resolution. Keep those paths inside the isolated fixture.
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=postgresql://fixture\nHAIL_ACTIVITY_DIGEST_API_KEY=test-key\n"
    )
    return subprocess.run(
        [
            "docker",
            "compose",
            "--project-directory",
            str(tmp_path),
            "--env-file",
            str(env_file),
            "-f",
            str(ROOT / "docker-compose.yml"),
            *(["-f", str(ROOT / overlay)] if overlay else []),
            "config",
            "--no-env-resolution",
            *args,
        ],
        env={"PATH": os.environ["PATH"], "COMPOSE_PROFILES": profile},
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker Compose is required")
@pytest.mark.parametrize(
    "overlay", [None, "docker-compose.local.yml", "docker-compose.prod.yml"]
)
def test_default_deployment_does_not_start_the_optional_worker(tmp_path, overlay):
    assert (
        "activity-digest"
        not in compose(tmp_path, "", "--services", overlay=overlay).splitlines()
    )
    assert (
        "activity-digest"
        in compose(
            tmp_path, "activity-digest", "--services", overlay=overlay
        ).splitlines()
    )


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker Compose is required")
@pytest.mark.parametrize(
    "overlay", [None, "docker-compose.local.yml", "docker-compose.prod.yml"]
)
def test_enabled_worker_reuses_api_image_without_ports_or_provider_credentials(
    tmp_path,
    overlay,
):
    config = json.loads(
        compose(tmp_path, "activity-digest", "--format", "json", overlay=overlay)
    )
    worker = config["services"]["activity-digest"]
    if overlay == "docker-compose.prod.yml":
        assert worker["image"] == config["services"]["api"]["image"]
        assert not worker.get("build")
        assert worker["restart"] == "unless-stopped"
        assert worker["logging"]["options"]["max-size"]
    else:
        assert worker["build"] == config["services"]["api"]["build"]
        assert not worker.get("image")
    assert worker["command"] == ["python", "-m", "hailhq.api.activity_digest"]
    assert not worker.get("ports")
    assert not worker.get("env_file")
    assert not worker.get("volumes")
    assert worker["depends_on"]["api"]["condition"] == "service_healthy"
    assert worker["stop_grace_period"] == "2m10s"
    assert worker["mem_limit"] == "268435456"
    assert worker["cpus"] == 0.25
    assert set(worker["environment"]) == {
        "DATABASE_URL",
        "HAIL_ACTIVITY_DIGEST_ENABLED",
        "HAIL_ACTIVITY_DIGEST_API_URL",
        "HAIL_ACTIVITY_DIGEST_API_KEY",
        "HAIL_ACTIVITY_DIGEST_FROM",
        "HAIL_ACTIVITY_DIGEST_TO",
    }
    assert worker["environment"]["HAIL_ACTIVITY_DIGEST_ENABLED"] == "false"
    assert worker["environment"]["HAIL_ACTIVITY_DIGEST_API_URL"] == "http://api:8080/v1"
