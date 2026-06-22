"""`edit` — exact string replacement, the tool whose design decides reliability.

The model supplies `old_string` (a substring of the file with enough surrounding
context to be unique) and `new_string`. The mechanic, and its whole point, is
that it fails *loudly and recoverably*:

  - zero matches   -> error: the string wasn't found (model retries)
  - many matches   -> error: the string isn't unique (model adds context)
  - exactly one    -> replace and write

That is deliberately not line-ranges (line numbers drift between read and write)
and not diffs (they fail to apply cleanly). Do not switch it. The error text is
returned to the model as the tool result so it self-corrects on the next pass.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from spine.tools.base import ToolResult


class EditParams(BaseModel):
    path: str = Field(description="Path to the file to edit.")
    old_string: str = Field(
        description="Exact text to replace. Must match exactly once in the file."
    )
    new_string: str = Field(description="Text to replace it with.")


class EditTool:
    name = "edit"
    description = (
        "Replace an exact string in a file. `old_string` must appear exactly once "
        "— include enough surrounding context to make it unique. Returns an error "
        "if it is not found or not unique, so you can retry."
    )
    parameters = EditParams

    def execute(self, args: EditParams) -> ToolResult:
        path = Path(args.path)
        if not path.exists():
            return ToolResult(f"File not found: {args.path}", is_error=True)
        if path.is_dir():
            return ToolResult(f"Path is a directory, not a file: {args.path}", is_error=True)

        if args.old_string == args.new_string:
            return ToolResult(
                "old_string and new_string are identical; nothing to change.",
                is_error=True,
            )

        text = path.read_text(encoding="utf-8")
        count = text.count(args.old_string)

        if count == 0:
            return ToolResult(
                f"old_string not found in {args.path}. The text must match exactly, "
                "including whitespace. Re-read the file and try again.",
                is_error=True,
            )
        if count > 1:
            return ToolResult(
                f"old_string is not unique in {args.path} ({count} matches). Add more "
                "surrounding context so it identifies exactly one location.",
                is_error=True,
            )

        path.write_text(text.replace(args.old_string, args.new_string), encoding="utf-8")
        return ToolResult(f"Edited {args.path} (1 replacement).")
