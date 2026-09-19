"""Substrate-owned definitions of substantive changes and birth-time artifacts."""

from fnmatch import fnmatchcase

from substrate.config import Config
from substrate.state_store import Status


def is_substantive(path: str, config: Config) -> bool:
    """Paths are POSIX-style and relative to agent/, including deleted files."""
    return not any(fnmatchcase(path, pat) for pat in config.completion_ignored_globs)


def ground_truth(config: Config, status: Status, last_good: str | None) -> str:
    # Count regular files on disk, never symlink targets outside the body. Walk
    # with Path.walk (which does not follow directory symlinks by default).
    count = 0
    for directory, dirs, files in config.agent_dir.walk():
        dirs[:] = [d for d in dirs if d not in {".git", ".venv", "venv"}]
        for name in files:
            file = directory / name
            path = file.relative_to(config.agent_dir).as_posix()
            if file.is_symlink() or not file.is_file():
                continue
            if is_substantive(path, config) and any(
                fnmatchcase(path, pat) for pat in config.artifact_globs
            ):
                count += 1
    return (
        "[Substrate-provided ground truth at birth — overrides MEMORY figures]\n"
        f"Generation (zero-based, blessed lives): {status.generation}\n"
        f"Source artifact files on disk: {count}\n"
        f"Count scope: {', '.join(config.artifact_globs)}; excludes configured "
        "state/generated paths, symlinks, .git and virtual environments. "
        "This is a file count, not a semantic count of works.\n"
        f"Lineage cost so far (USD): {status.budget_spent_usd:.6f}\n"
        f"last_good: {last_good or '(not yet set)'}\n"
    )
