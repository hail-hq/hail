"""The workflow path filters never skip a file the services use.

``deploy.yml`` skips pushes that change only ``paths-ignore`` files, and
``ci.yml`` skips such pull requests. A pattern that matched a file an image
copies, a file the VM mounts, or a Python file would ship or merge it
untested or undeployed. Markdown is left out: no service reads it.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


def _tracked() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("needs a git checkout")
    return out.splitlines()


def _glob(pattern: str) -> re.Pattern[str]:
    """GitHub's path glob: ``**`` crosses folders, ``*`` does not."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(f"^{out}$")


def _ignored(workflow: str, event: str) -> list[re.Pattern[str]]:
    data = yaml.safe_load((WORKFLOWS / workflow).read_text())
    on = data.get(True, data.get("on"))
    return [_glob(p) for p in on[event]["paths-ignore"]]


def _deploy_sources() -> set[str]:
    """Paths the images copy, the VM mounts, and the deploy script runs."""
    sources = {"scripts/deploy"}
    for dockerfile in ROOT.glob("*/Dockerfile"):
        sources.add(str(dockerfile.relative_to(ROOT)))
        for line in dockerfile.read_text().splitlines():
            parts = line.split()
            if not parts or parts[0] not in ("COPY", "ADD") or "--from=" in line:
                continue
            args = [p for p in parts[1:] if not p.startswith("--")]
            sources.update(args[:-1])
    for compose in ROOT.glob("docker-compose*.yml"):
        sources.add(compose.name)
        text = compose.read_text()
        sources.update(re.findall(r"- \./([^:\s]+):", text))
        sources.update(re.findall(r"source: \./(\S+)", text))
    return {s.rstrip("/") for s in sources}


def _hits(files: list[str], patterns: list[re.Pattern[str]]) -> list[str]:
    return [f for f in files if any(p.match(f) for p in patterns)]


def _used(files: list[str], sources: set[str]) -> list[str]:
    # Images copy Markdown (costs/README.md) but no service reads it.
    return [
        f
        for f in files
        if any(f == s or f.startswith(f"{s}/") for s in sources)
        and not f.endswith(".md")
    ]


def test_deploy_never_skips_what_the_services_use():
    sources = _deploy_sources()
    assert {"costs", "Caddyfile", "api/migrations"} <= sources
    used = _used(_tracked(), sources)
    assert used
    assert _hits(used, _ignored("deploy.yml", "push")) == []


def test_ci_never_skips_python_or_costs():
    files = _tracked()
    # The docker job builds what the images copy; the go job builds cli/;
    # the sdk and mcp tests read openapi/.
    checked = _used(files, _deploy_sources() | {"cli", "openapi"}) + [
        f
        for f in files
        if f.endswith((".py", ".toml", ".lock", "Dockerfile"))
        or (f.startswith(("costs/", ".github/workflows/")) and not f.endswith(".md"))
    ]
    assert _hits(checked, _ignored("ci.yml", "pull_request")) == []
