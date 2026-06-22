"""`terminate` — the agent's one new tool: request the end of this generation.

This is the single agent-facing primitive Kaizen adds to spine's four. It does
*not* kill anything. Process lifecycle belongs to the runner (the substrate),
which the agent cannot reach. All this tool does is *record a request* into a
sink the runner reads after the agent's turn ends, and raise spine's `terminate`
hint so the loop stops cleanly. The runner then performs the termination protocol
(persist -> commit -> boot-check -> bless -> wake) — or refuses it.

Keeping "request" and "perform" on opposite sides of the agent/substrate boundary
is invariants #5/#6: the agent asks to die; it never defines what dying does, and
it never schedules its own rebirth. Even if the agent edits this file, it can only
change how it *asks* — the runner still owns what death means.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field

from spine.tools.base import ToolResult


@dataclass
class TerminationRequest:
    """The sink the runner injects and reads back. The tool only fills it in.

    Defined here (in the body) so the tool stays importable with no substrate
    dependency — `import spine` must succeed in a bare checkout for the boot-check.
    The runner imports this type to construct the sink it hands to the tool.
    """

    requested: bool = False
    reason: str = ""
    wake_after: float | None = None
    roadmap_complete: bool = False


class TerminateParams(BaseModel):
    reason: str = Field(
        description="Why this generation is ending — a short note for the journal "
        "and for the next generation that inherits your memory."
    )
    wake_after: float | None = Field(
        default=None,
        ge=0,
        description="Seconds from now until the next generation should be reborn. "
        "Omit (or null) to respawn immediately.",
    )
    roadmap_complete: bool = Field(
        default=False,
        description="Set true ONLY if the entire ROADMAP is finished. The lineage "
        "then halts instead of respawning — this is how the loop is allowed to win.",
    )


class TerminateTool:
    name = "terminate"
    description = (
        "Request the end of this generation. Use this once you have made and "
        "verified enough progress for one life: the runner will persist the "
        "lineage's memory, commit and boot-check your work, and schedule the next "
        "generation. You do not kill the process yourself — the runner performs the "
        "shutdown, and may refuse it if your body fails its boot-check. Optionally "
        "pass wake_after (seconds until rebirth) or roadmap_complete."
    )
    parameters = TerminateParams

    def __init__(self, sink: TerminationRequest) -> None:
        self._sink = sink

    def execute(self, args: TerminateParams) -> ToolResult:
        self._sink.requested = True
        self._sink.reason = args.reason
        self._sink.wake_after = args.wake_after
        self._sink.roadmap_complete = args.roadmap_complete

        if args.roadmap_complete:
            note = "Termination requested with roadmap_complete=true; the lineage will halt."
        elif args.wake_after:
            note = (
                f"Termination requested; rebirth in ~{args.wake_after:g}s. "
                f"Reason: {args.reason}"
            )
        else:
            note = f"Termination requested; immediate rebirth. Reason: {args.reason}"
        return ToolResult(note, terminate=True)
