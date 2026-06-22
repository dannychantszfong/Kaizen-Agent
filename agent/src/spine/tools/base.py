"""The Tool protocol — the entire contract every tool satisfies.

A tool is four things: a `name`, a `description`, a Pydantic `parameters`
schema, and an `execute()` that takes a validated args model and returns a
`ToolResult`. The four built-ins satisfy it; a clone's tools satisfy it; and —
deliberately — an `Agent` could satisfy it too (see spine-spec.md §8). Do not
add anything here that an agent could not also provide.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pydantic import BaseModel


@dataclass
class ToolResult:
    """What a tool hands back to the loop.

    `output` is the text the model sees. `is_error` marks a recoverable failure
    (a bad path, a not-unique edit) — still returned to the model, never raised,
    so it can self-correct. `terminate` is the optional "stop after this batch"
    hint; it only takes effect if every tool result in the batch agrees.
    """

    output: str
    is_error: bool = False
    terminate: bool = False


@runtime_checkable
class Tool(Protocol):
    name: str
    description: str
    parameters: type[BaseModel]

    def execute(self, args: BaseModel) -> ToolResult: ...
