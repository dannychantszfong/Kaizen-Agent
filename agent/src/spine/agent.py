"""The agent loop — the core you own.

This file is the whole backbone above the tools and the provider. It holds the
conversation state, calls the model, dispatches tool calls, validates their
arguments, runs hooks, and feeds results back. It knows nothing about *which*
model it talks to (that's `provider.complete`) and nothing about *what* a tool
does (tools are just typed callables). Read it top to bottom — it is meant to be
understood line by line.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from spine import provider
from spine.hooks import Hooks
from spine.tools.base import Tool, ToolResult

# Signature of the provider seam. Swap in a stub with this shape to drive the
# loop without real API calls (see tests/test_loop.py).
CompleteFn = Callable[
    [str, list[dict[str, Any]], list[dict[str, Any]] | None],
    provider.Completion,
]

_DEFAULT_SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "system.md").read_text(
    encoding="utf-8"
)


class Agent:
    """A single agent: one model, a set of tools, and the loop that joins them."""

    def __init__(
        self,
        model: str,
        tools: list[Tool],
        *,
        system_prompt: str | None = None,
        hooks: Hooks | None = None,
        complete: CompleteFn | None = None,
        max_iterations: int = 50,
    ) -> None:
        self.model = model
        self.tools: dict[str, Tool] = {t.name: t for t in tools}
        self.system_prompt = system_prompt if system_prompt is not None else _DEFAULT_SYSTEM_PROMPT
        self.hooks = hooks or Hooks()
        self.complete = complete or provider.complete
        self.max_iterations = max_iterations

        # Conversation transcript and a free-form scratch space that hooks /
        # memory extensions can dock onto (a seam, empty by default).
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt}
        ]
        self.state: dict[str, Any] = {}

    # -- public API ---------------------------------------------------------

    def run(self, user_message: str) -> str:
        """Run one user turn to completion and return the agent's final text.

        "Completion" means the model stopped asking for tools (or a tool batch
        unanimously raised the `terminate` hint, or we hit `max_iterations`).
        """
        self.messages.append({"role": "user", "content": user_message})
        self.hooks.session_start(self)
        try:
            return self._loop()
        finally:
            self.hooks.session_end(self)

    @property
    def tool_schemas(self) -> list[dict[str, Any]]:
        """The tools rendered as OpenAI-format function schemas for the provider."""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters.model_json_schema(),
                },
            }
            for tool in self.tools.values()
        ]

    # -- the loop ------------------------------------------------------------

    def _loop(self) -> str:
        for _ in range(self.max_iterations):
            completion = self.complete(self.model, self.messages, self.tool_schemas)
            self.messages.append(completion.to_message())

            # No tool calls => the model is done with this turn.
            if not completion.tool_calls:
                return completion.content or ""

            terminate_votes: list[bool] = []
            for call in completion.tool_calls:
                result = self._dispatch(call)
                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": result.output,
                    }
                )
                terminate_votes.append(result.terminate)

            # The terminate hint only fires if the whole batch agrees.
            if terminate_votes and all(terminate_votes):
                return completion.content or ""

        return (
            "Stopped: reached the maximum of "
            f"{self.max_iterations} iterations without finishing."
        )

    # -- one tool call -------------------------------------------------------

    def _dispatch(self, call: provider.ToolCall) -> ToolResult:
        """Validate, hook, execute, hook — returning a ToolResult either way.

        Every failure path here returns a ToolResult (it becomes the tool message
        the model reads next turn). Nothing in normal operation raises; that is
        what lets the model self-correct.
        """
        tool = self.tools.get(call.name)
        if tool is None:
            return ToolResult(f"Unknown tool: {call.name}", is_error=True)

        # Validate args against the tool's Pydantic schema. A failure is fed back
        # to the model as the result, not raised (spec §5).
        try:
            args = tool.parameters.model_validate(call.arguments)
        except ValidationError as e:
            return ToolResult(f"Invalid arguments for {call.name}:\n{e}", is_error=True)

        decision = self.hooks.before_tool_call(tool, args, self)
        if decision.blocked:
            return ToolResult(decision.message or f"Blocked: {call.name}", is_error=True)
        if decision.args is not None:
            args = decision.args

        result = tool.execute(args)
        return self.hooks.after_tool_call(tool, args, result, self)
