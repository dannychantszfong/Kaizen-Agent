"""`write` — create a new file or overwrite an existing one with full contents.

`write` clobbers. For changes to existing files, prefer `edit`. A clone can add
a "refuse to overwrite without confirmation" `before_tool_call` hook; the core
does not.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from spine.tools.base import ToolResult


class WriteParams(BaseModel):
    path: str = Field(description="Path to write. Parent directories are created.")
    content: str = Field(description="Full file contents. Overwrites any existing file.")


class WriteTool:
    name = "write"
    description = (
        "Write full contents to a file, creating it or overwriting it. Creates "
        "parent directories as needed. Prefer `edit` for changes to existing files."
    )
    parameters = WriteParams

    def execute(self, args: WriteParams) -> ToolResult:
        path = Path(args.path)
        if path.is_dir():
            return ToolResult(f"Path is a directory, not a file: {args.path}", is_error=True)

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(args.content, encoding="utf-8")
        except OSError as e:
            return ToolResult(f"Could not write {args.path}: {e}", is_error=True)

        n = len(args.content.splitlines())
        return ToolResult(f"Wrote {n} line(s) to {args.path}")
