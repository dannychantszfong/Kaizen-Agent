"""Shared fixtures for the M0 acceptance checks.

Every check runs against a *throwaway* git repo — a copy of this project with a
fresh history — so the real repo is never committed to, reset, or rolled back. The
substrate is entirely path-parameterized off `Config.root`, which is what makes
that possible.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

# Pin `spine` to the real body up front, so a temp repo's `agent/src` (inserted on
# sys.path while a body runs) can never shadow it in-process. The boot-check runs
# the candidate body in a *subprocess*, which is the only place a temp body loads.
import spine  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parents[1]

_IGNORE = shutil.ignore_patterns(
    ".git",
    "__pycache__",
    "*.pyc",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "state",
    "journal",
    "kaizen-bootcheck-*",
    ".venv",
    "venv",
    "*.egg-info",
    "runs",
    "*-export",
    ".tmp*",
    "*.log",
    "a",
    "_idx.txt",
    "_wrk.txt",
    "existing_works.txt",
    "index_ids*.txt",
    "work_files*.txt",
    ".env",
)


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True, check=True
    )


def head_sha(root: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()


@pytest.fixture
def lineage_repo(tmp_path: Path) -> Path:
    """A fresh, self-contained lineage repo with a known-good initial body."""
    root = tmp_path / "lineage"
    shutil.copytree(REPO_ROOT, root, ignore=_IGNORE)

    _run("git", "init", "-q", "-b", "main", cwd=root)
    _run("git", "config", "user.email", "kaizen@test", cwd=root)
    _run("git", "config", "user.name", "kaizen-test", cwd=root)
    _run("git", "config", "commit.gpgsign", "false", cwd=root)
    _run("git", "add", "-A", cwd=root)
    _run("git", "commit", "-q", "-m", "initial body (known good)", cwd=root)
    return root
