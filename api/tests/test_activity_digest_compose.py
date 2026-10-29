"""Exercise the real Compose parser without starting services or reading .env."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def compose(tmp_path, profile, *args):
    env_file = tmp_path / "compose.env"
    env_file.write_text(
        "DATABASE_URL=postgresql://fixture\nHAIL_ACTIVITY_DIGEST_API_KEY=test-key\n"
    )
    return subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(env_file),
            "-f",
            str(ROOT / "docker-compose.yml"),
            "-f",
            str(ROOT / "docker-compose.prod.yml"),
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
def test_default_deployment_does_not_start_the_optional_worker(tmp_path):
    assert "activity-digest" not in compose(tmp_path, "", "--services").splitlines()
    assert (
        "activity-digest"
        in compose(tmp_path, "activity-digest", "--services").splitlines()
    )


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker Compose is required")
def test_enabled_worker_reuses_api_image_without_ports_or_provider_credentials(
    tmp_path,
):
    config = json.loads(compose(tmp_path, "activity-digest", "--format", "json"))
    worker = config["services"]["activity-digest"]
    assert worker["image"] == config["services"]["api"]["image"]
    assert worker["command"] == ["python", "-m", "hailhq.api.activity_digest"]
    assert not worker.get("ports")
    assert not worker.get("env_file")
    assert not worker.get("volumes")
    assert worker["restart"] == "unless-stopped"
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
    assert worker["logging"]["options"]["max-size"]
