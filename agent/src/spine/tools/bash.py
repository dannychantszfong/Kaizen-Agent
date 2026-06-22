"""`bash` — run a shell command. The escape hatch for everything else.

grep, git, curl, running tests, moving files, installing a package: anything the
other three tools don't cover, the model does here. Runs as the launching user
with a timeout and truncated output. There are no in-core permission checks —
that is the security boundary called out in the spec (§1, §6); isolation is
external, and a clone restricts this through a `before_tool_call` hook.
"""

from __future__ import annotations

import subprocess

from pydantic import BaseModel, Field

from spine.tools.base import ToolResult

MAX_OUTPUT = 30_000  # characters per stream before truncation


class BashParams(BaseModel):
    command: str = Field(description="Shell command to run.")
    timeout: int = Field(
        default=120, ge=1, le=600, description="Timeout in seconds (max 600)."
    )


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    return text[:MAX_OUTPUT] + f"\n... [truncated {len(text) - MAX_OUTPUT} chars]"


class BashTool:
    name = "bash"
    description = (
        "Run a shell command and return its stdout, stderr, and exit code. The "
        "escape hatch for anything the other tools don't cover (grep, git, curl, "
        "tests, package installs). Output is truncated if very large."
    )
    parameters = BashParams

    def execute(self, args: BashParams) -> ToolResult:
        try:
            proc = subprocess.run(
                args.command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=args.timeout,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                f"Command timed out after {args.timeout}s.", is_error=True
            )

        parts = [f"exit code: {proc.returncode}"]
        if proc.stdout:
            parts.append("stdout:\n" + _truncate(proc.stdout))
        if proc.stderr:
            parts.append("stderr:\n" + _truncate(proc.stderr))
        return ToolResult("\n".join(parts), is_error=proc.returncode != 0)
