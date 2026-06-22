"""`read` — read a file's contents, optionally a page of it.

Returns text with 1-based line numbers (cat -n style) so the model can refer to
locations and gather enough surrounding context to make `edit` reliable.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from spine.tools.base import ToolResult


class ReadParams(BaseModel):
    path: str = Field(description="Path to the file to read.")
    offset: int | None = Field(
        default=None, ge=1, description="1-based line number to start reading from."
    )
    limit: int | None = Field(
        default=None, ge=1, description="Maximum number of lines to read."
    )


class ReadTool:
    name = "read"
    description = (
        "Read a file from the filesystem. Returns its contents with 1-based line "
        "numbers. Use offset/limit to page through large files."
    )
    parameters = ReadParams

    def execute(self, args: ReadParams) -> ToolResult:
        path = Path(args.path)
        if not path.exists():
            return ToolResult(f"File not found: {args.path}", is_error=True)
        if path.is_dir():
            return ToolResult(f"Path is a directory, not a file: {args.path}", is_error=True)

        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            return ToolResult(f"File is not valid UTF-8 text: {args.path}", is_error=True)

        start = (args.offset or 1) - 1
        end = start + args.limit if args.limit is not None else len(lines)
        window = lines[start:end]

        if not window:
            return ToolResult(f"(no lines in requested range of {args.path})")

        numbered = "\n".join(
            f"{start + i + 1:6d}\t{line}" for i, line in enumerate(window)
        )
        return ToolResult(numbered)
