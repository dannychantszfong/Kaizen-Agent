"""spine — a minimal agent backbone: a language model, four tools, and a loop.

The whole public surface is small on purpose. A clone imports `Agent`, the four
built-in tools, and (optionally) `Hooks`, and builds upward from there.
"""

from spine.agent import Agent
from spine.hooks import BeforeToolCall, Hooks
from spine.tools import BashTool, EditTool, ReadTool, WriteTool, default_tools
from spine.tools.base import Tool, ToolResult

__all__ = [
    "Agent",
    "Hooks",
    "BeforeToolCall",
    "Tool",
    "ToolResult",
    "ReadTool",
    "WriteTool",
    "EditTool",
    "BashTool",
    "default_tools",
]
