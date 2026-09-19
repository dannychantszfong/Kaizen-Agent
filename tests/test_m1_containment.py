"""M1 containment acceptance checks - the OS-level agent<->substrate boundary.

Two tiers:

  - `test_compose_config_is_valid` parses the compose file with the docker CLI
    (no daemon needed), so the containment topology is checked on every run.
  - the build/run proofs require a live Docker daemon AND opt-in via
    KAIZEN_CONTAINER_TESTS=1, because they build an image and start containers.
    They are the file-permission proof the done-definition asks for ("a
    file-permission test, not just the hook") and the volume-persistence proof
    ("state survives docker rm + rebuild"). Without a daemon they skip cleanly.

Run the heavy tier with:
    KAIZEN_CONTAINER_TESTS=1 pytest tests/test_m1_containment.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE = REPO_ROOT / "containment" / "docker-compose.yml"
DOCKERFILE = REPO_ROOT / "containment" / "Dockerfile"
EGRESS_FILTER = REPO_ROOT / "containment" / "egress" / "filter"
IMAGE = "kaizen-containment-test:latest"


def _docker_cli() -> bool:
    return shutil.which("docker") is not None


def _docker_daemon() -> bool:
    if not _docker_cli():
        return False
    try:
        return (
            subprocess.run(
                ["docker", "info"], capture_output=True, timeout=30, check=False
            ).returncode
            == 0
        )
    except Exception:  # noqa: BLE001
        return False


# --- always-on: the compose topology parses and is shaped right --------------


@pytest.mark.skipif(not _docker_cli(), reason="docker CLI not installed")
def test_compose_config_is_valid() -> None:
    proc = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "config"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    rendered = proc.stdout
    # The body is on an internet-less network; egress goes through the proxy only.
    assert "internal: true" in rendered
    assert "egress-proxy" in rendered
    # The privilege-drop + hardening survived rendering.
    assert "gosu agent" in rendered
    assert "no-new-privileges:true" in rendered
    # The lineage's durable home is a named volume.
    assert "lineage" in rendered


# --- always-on: the egress allowlist is a real named list, not allow-all ------


def test_egress_allowlist_is_named_and_not_open() -> None:
    raw = EGRESS_FILTER.read_text(encoding="utf-8")
    # tinyproxy strips from '#' to end-of-line; the live rules are the rest.
    rules = [
        line.split("#", 1)[0].strip()
        for line in raw.splitlines()
        if line.split("#", 1)[0].strip()
    ]
    assert rules, "the allowlist must have at least one rule"

    # Every enabled provider's exact API host is present and anchored (^...$).
    expected_hosts = {
        "api.anthropic.com",
        "api.openai.com",
        "generativelanguage.googleapis.com",
        "api.deepseek.com",
        "openrouter.ai",
    }
    literal = {r.replace("\\", "").lstrip("^").rstrip("$") for r in rules}
    assert expected_hosts <= literal, literal
    assert all(r.startswith("^") and r.endswith("$") for r in rules), rules

    # And it is NOT an allow-all: no catch-all / bare-dot / unanchored wildcard.
    for bad in (".*", ".", "^.*$", "^.*", ".*$"):
        assert bad not in rules, f"allowlist must not contain catch-all {bad!r}"


# --- opt-in heavy tier: real build + container boundary proofs ---------------

_heavy = pytest.mark.skipif(
    not _docker_daemon() or os.environ.get("KAIZEN_CONTAINER_TESTS") != "1",
    reason="needs a running Docker daemon and KAIZEN_CONTAINER_TESTS=1 (opt-in)",
)


@pytest.fixture(scope="module")
def image() -> str:
    subprocess.run(
        ["docker", "build", "-f", str(DOCKERFILE), "-t", IMAGE, str(REPO_ROOT)],
        check=True,
    )
    yield IMAGE
    subprocess.run(["docker", "image", "rm", "-f", IMAGE], check=False)


def _as_agent(image: str, script: str, *, volume: str | None = None):
    """Run `sh -c script` as the unprivileged `agent` uid in a throwaway container."""
    cmd = ["docker", "run", "--rm"]
    if volume:
        cmd += ["-v", f"{volume}:/lineage"]
    cmd += ["--entrypoint", "gosu", image, "agent", "sh", "-c", script]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


@_heavy
def test_agent_cannot_write_outside_its_tree(image: str) -> None:
    # The body's own tree is writable.
    ok = _as_agent(image, "echo hi > /lineage/agent/proof.txt && echo WROTE")
    assert "WROTE" in ok.stdout, ok.stderr

    # The immortal supervisor and the brakes are NOT (root-owned, read-only).
    for immortal in (
        "/lineage/runner.py",
        "/lineage/watchdog.py",
        "/lineage/substrate/guardrail.py",
        "/lineage/substrate/config.py",
    ):
        r = _as_agent(image, f"echo x >> {immortal} && echo WROTE || echo DENIED")
        assert "DENIED" in r.stdout and "WROTE" not in r.stdout, (
            f"{immortal}: {r.stdout}"
        )

    # Nor anything on the host-side rootfs.
    r = _as_agent(image, "echo x > /etc/kaizen_escape && echo WROTE || echo DENIED")
    assert "DENIED" in r.stdout and "WROTE" not in r.stdout

    # But the runtime state the runner must persist IS agent-writable (invariant #4).
    s = _as_agent(image, "echo ok > /lineage/substrate/state/heartbeat && echo WROTE")
    assert "WROTE" in s.stdout, s.stderr


@_heavy
def test_state_survives_container_removal(image: str) -> None:
    vol = "kaizen-test-lineage"
    subprocess.run(["docker", "volume", "rm", "-f", vol], check=False)
    try:
        # First container writes a marker into canonical state, then is removed (--rm).
        w = _as_agent(
            image,
            "echo gen-marker > /lineage/substrate/state/MEMORY.md",
            volume=vol,
        )
        assert w.returncode == 0, w.stderr
        # A brand-new container on the same named volume still sees it.
        r = _as_agent(image, "cat /lineage/substrate/state/MEMORY.md", volume=vol)
        assert "gen-marker" in r.stdout, r.stderr
    finally:
        subprocess.run(["docker", "volume", "rm", "-f", vol], check=False)


@_heavy
def test_image_seed_has_no_prior_run_artifacts(image: str) -> None:
    # Override the entrypoint: inspect the image without starting a watchdog/body.
    script = """
from pathlib import Path
from fnmatch import fnmatchcase
import subprocess
root = Path('/lineage')
bad_names = ('a', '*.log', '_idx.txt', '_wrk.txt', 'existing_works.txt',
             'index_ids*.txt', 'work_files*.txt', '*-export', '*.pyc',
             '__pycache__', '.pytest_cache', '.ruff_cache')
for path in root.rglob('*'):
    rel = path.relative_to(root)
    if '.git' in rel.parts:
        continue
    assert not any(fnmatchcase(path.name, pat) for pat in bad_names), rel
    if path.name == 'journal':
        assert rel.as_posix() == 'substrate/journal', rel
        assert not list(path.iterdir()), 'runtime journal must be empty'
assert not list((root / 'substrate/state').iterdir())
for name in ('runs', 'JOURNAL.md', 'agent/JOURNAL.md', 'agent/MEMORY.md',
             'agent/ROADMAP.md', 'agent/TODO.json'):
    assert not (root / name).exists(), name
tracked = subprocess.check_output(['git', 'ls-files'], cwd=root, text=True).splitlines()
assert '.gitignore' in tracked and 'agent/.gitignore' in tracked
assert not any('journal' in Path(p).parts for p in tracked)
print('clean image seed verified')
"""
    proc = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "python",
            image,
            "-c",
            script,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
