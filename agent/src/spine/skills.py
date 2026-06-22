"""Skills — capability-as-documentation, discovered and surfaced to the model.

A skill is a directory with a `README.md`. The README documents a CLI tool (or a
procedure) the agent runs through `bash`. There is no plugin protocol: discovery
just finds the directories and the model reads/invokes them itself. This is a
seam, not a feature — a clone points the agent at a skills root and the prompt
gains a short catalogue.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class Skill:
    name: str
    path: Path
    description: str  # first non-empty line of the skill's README


def discover_skills(root: str | Path) -> list[Skill]:
    """Find skills under `root`: each subdirectory containing a README.md."""
    root = Path(root)
    if not root.is_dir():
        return []

    skills: list[Skill] = []
    for child in sorted(root.iterdir()):
        readme = child / "README.md"
        if child.is_dir() and readme.is_file():
            description = ""
            for line in readme.read_text(encoding="utf-8").splitlines():
                stripped = line.strip().lstrip("# ").strip()
                if stripped:
                    description = stripped
                    break
            skills.append(Skill(name=child.name, path=child, description=description))
    return skills


def skills_prompt(skills: list[Skill]) -> str:
    """Render a catalogue for the system prompt. Empty string if no skills."""
    if not skills:
        return ""
    lines = [
        "You have skills available. Each is a directory with a README.md "
        "describing how to use it — read the README, then run it via `bash`:",
    ]
    for s in skills:
        lines.append(f"- {s.name} ({s.path}): {s.description}")
    return "\n".join(lines)
