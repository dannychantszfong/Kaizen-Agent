"""Hooks — the extension point that keeps the core at four tools.

Rather than baking features (permissions, confirmations, logging, memory) into
the loop, the core exposes points where a clone injects behavior. The default
implementations ship **permissive**: every `before_tool_call` allows. The
mechanism exists; the policy is empty. A clone subclasses `Hooks` and overrides
only what it needs, without touching the core.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from spine.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from spine.agent import Agent


@dataclass
class BeforeToolCall:
    """A `before_tool_call` decision.

    Default (no fields set) means **allow**. Set `blocked=True` with a `message`
    to stop the call and return that message to the model instead of running the
    tool. Set `args` to a replacement model to run the tool with modified args.
    """

    blocked: bool = False
    message: str = ""
    args: BaseModel | None = None


class Hooks:
    """Permissive, no-op hooks. Subclass and override to add policy."""

    def session_start(self, agent: "Agent") -> None:
        """Called once before the first turn. Register tools, load skills, etc."""

    def session_end(self, agent: "Agent") -> None:
        """Called once when a run finishes. Teardown, flush logs, etc."""

    def before_tool_call(
        self, tool: Tool, args: BaseModel, agent: "Agent"
    ) -> BeforeToolCall:
        """Runs before a tool executes. The only place policy belongs.

        This is where a clone puts a confirmation prompt, an allowlist, an
        `rm -rf` guard, or audit logging. Ships allowing everything.
        """
        return BeforeToolCall()

    def after_tool_call(
        self, tool: Tool, args: BaseModel, result: ToolResult, agent: "Agent"
    ) -> ToolResult:
        """Runs after a tool executes. Inspect or annotate the result; may set
        the `terminate` hint by returning a result with `terminate=True`."""
        return result
