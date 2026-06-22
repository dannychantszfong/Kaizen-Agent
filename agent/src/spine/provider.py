"""The rented provider layer — the one place the core touches an LLM SDK.

Everything above this file is provider-agnostic. The agent loop speaks in
normalized `Completion`/`ToolCall` objects and OpenAI-style message dicts, and
never imports `litellm` (or any provider SDK) directly. If you ever swap
`litellm` for raw SDKs, this is the only file that changes.

`complete()` has the exact shape the loop depends on:

    complete(model, messages, tools) -> Completion

A test (or a clone) can supply any callable with that signature instead — see
`tests/test_loop.py`, which drives the loop with a stub and makes no API calls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    """One tool invocation the model asked for, with arguments already parsed."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Completion:
    """A normalized assistant turn: free text plus any tool calls."""

    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)

    def to_message(self) -> dict[str, Any]:
        """Render this turn as an OpenAI-style assistant message for the transcript."""
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments),
                    },
                }
                for tc in self.tool_calls
            ]
        return msg


def complete(
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> Completion:
    """Call the model through litellm and normalize the response.

    `tools` is a list of OpenAI-format function schemas (see `Agent.tool_schemas`).
    Credentials resolve from the environment, the same as litellm's defaults.
    """
    import litellm  # imported lazily so the core imports without the SDK present

    response = litellm.completion(
        model=model,
        messages=messages,
        tools=tools or None,
        tool_choice="auto" if tools else None,
    )
    message = response.choices[0].message

    tool_calls: list[ToolCall] = []
    for tc in message.tool_calls or []:
        try:
            arguments = json.loads(tc.function.arguments or "{}")
        except json.JSONDecodeError:
            # Leave it for Pydantic validation to reject and feed back to the model.
            arguments = {"__raw__": tc.function.arguments}
        tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=arguments))

    return Completion(content=message.content, tool_calls=tool_calls)
