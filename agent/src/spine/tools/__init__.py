"""The four built-in tools. There is no fifth — `bash` is the escape hatch."""

from spine.tools.bash import BashTool
from spine.tools.base import Tool, ToolResult
from spine.tools.edit import EditTool
from spine.tools.read import ReadTool
from spine.tools.write import WriteTool


def default_tools() -> list[Tool]:
    """The four built-ins, freshly instantiated. The whole backbone."""
    return [ReadTool(), WriteTool(), EditTool(), BashTool()]


__all__ = [
    "Tool",
    "ToolResult",
    "ReadTool",
    "WriteTool",
    "EditTool",
    "BashTool",
    "default_tools",
]
