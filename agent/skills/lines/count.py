#!/usr/bin/env python3
"""Count lines and files per extension under a directory.

A worked example of a spine skill: a small, self-contained CLI the agent runs via
`bash`, documented in the sibling README.md. Standard library only.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules", ".pytest_cache"}


def count(root: Path) -> tuple[Counter[str], Counter[str]]:
    files: Counter[str] = Counter()
    lines: Counter[str] = Counter()
    for path in root.rglob("*"):
        if not path.is_file() or SKIP_DIRS & set(path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # not text, or unreadable — skip it
        ext = path.suffix or "(none)"
        files[ext] += 1
        lines[ext] += len(text.splitlines())
    return files, lines


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        raise SystemExit(1)

    files, lines = count(root)
    print(f"{'ext':<12}{'files':>7}{'lines':>9}")
    for ext, _ in lines.most_common():
        print(f"{ext:<12}{files[ext]:>7}{lines[ext]:>9}")
    print("-" * 28)
    print(f"{'total':<12}{sum(files.values()):>7}{sum(lines.values()):>9}")


if __name__ == "__main__":
    main()
