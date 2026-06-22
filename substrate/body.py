"""substrate.body - how the runner brings a generation's body to life.

The runner (the womb) imports the spine `Agent` from the *editable* body tree and
drives it in-process - that coupling is the whole point of a runner, and it is made
safe by the boot-check (a body that breaks `import spine` never gets blessed) and by
`last_good` rollback. This module is the single seam where that wiring lives: add
`agent/src` to the path, assemble the four tools + `terminate`, attach the
substrate's guardrail hook, and run the body.

Everything a body needs from the substrate arrives in a `BodyContext` the runner
builds: the config, the termination sink, the guardrail/checkpoint hooks, a logger,
the generation number, and a `complete()` that meters real provider spend. The real
body (`run_real_body`) is the M1 default; the M0 stub bodies remain, selectable via
`KAIZEN_M0_BODY`, so the whole flywheel is still provable offline with zero spend.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable

from substrate.config import Config


@dataclass
class BodyContext:
    """Everything the runner hands a body for one life. The body never reaches
    past this into the substrate; the runner reads `sink` back when the turn ends
    and reads the meter (closed over by `complete`) to charge the budget."""

    config: Config
    sink: Any  # TerminationRequest: the body fills it, the runner performs it
    hooks: Any  # SubstrateHooks: the bash guardrail + the continuous state mirror
    log: Callable[[str], None]
    generation: int
    complete: Any  # metered `complete(model, messages, tools)` the runner injects
    # () -> True once this generation's spend has reached the cap. The runner closes
    # it over the live meter; the turn loop checks it so a productive-but-never-
    # terminating generation can't burn unbounded budget before the watchdog (which
    # only checks caps between generations) regains control. Defaults to "never".
    over_budget: Callable[[], bool] = lambda: False
    # Set by the body when it ends a generation because the budget cap was reached
    # mid-run; the runner reads it and HALTS the lineage cleanly (sentinel) rather
    # than ending into a respawn.
    budget_halt: bool = False


# A body runner drives exactly one generation from a BodyContext.
BodyRunner = Callable[["BodyContext"], None]

# The substrate-injected nudge: asked at the stopped-without-terminating boundary
# (event-based, no timer), so thinking-model latency never triggers it.
_NUDGE_PROMPT = (
    "You ended your turn without terminating. If you have more to do, continue. "
    "If you're finished, call terminate."
)

_BIRTH_PROMPT = (
    "A new generation has begun. Read your MEMORY.md, ROADMAP.md, and TODO.json, "
    "orient yourself on where the last generation left off, then follow your "
    "rhythm. Persist progress to those files as you go - you may die at any moment "
    "- and terminate once you have made a clean, verified increment."
)


def ensure_body_importable(config: Config) -> None:
    """Put the editable body's `src` on the path so `import spine` resolves to it."""
    p = str(config.agent_src)
    if p not in sys.path:
        sys.path.insert(0, p)


def resolve_model(config: Config) -> str:
    """The model this generation runs on: the body's own choice if it wrote one
    (agent/MODEL), else the substrate default. Read fresh each birth so a model the
    agent picked last life takes effect this life. Metering prices whatever this is
    off the real response, so the choice carries its own provider."""
    p = config.model_choice_path
    if p.exists():
        choice = p.read_text(encoding="utf-8").strip()
        if choice:
            return choice
    return config.model


def build_agent(config: Config, *, sink, hooks, complete):  # noqa: ANN001
    """Assemble the body: four tools + `terminate`, the guardrail hook, the prompt.

    The system prompt is read VERBATIM from the body's own `prompts/system.md`, so
    each generation runs on its current (possibly self-evolved) prompt and the
    human-authored PRIME DIRECTIVE is never rewritten by the substrate. Carry-over
    context is delivered as seeded files in agent/ (which the prompt tells the body
    to read), not by concatenation here.
    """
    ensure_body_importable(config)
    from spine import Agent, default_tools
    from spine.tools.terminate import TerminateTool

    system_prompt = (config.agent_src / "spine" / "prompts" / "system.md").read_text(
        encoding="utf-8"
    )
    tools = [*default_tools(), TerminateTool(sink)]
    return Agent(
        model=resolve_model(config),
        tools=tools,
        hooks=hooks,
        complete=complete,
        system_prompt=system_prompt,
    )


def make_stub_provider(config: Config):
    """A deterministic `complete`-shaped callable for the offline M0 bodies.

    Scripts one generation: attempt an escape via `bash` (to exercise the
    guardrail in the live path), then call `terminate`. It ignores tool results and
    replays queued completions - the same pattern the body's own loop tests use.
    """
    ensure_body_importable(config)
    from spine.provider import Completion, ToolCall

    queue = [
        Completion(
            content=None,
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="bash",
                    arguments={"command": "echo escape > ../escape.txt"},
                )
            ],
        ),
        Completion(
            content="done for this life",
            tool_calls=[
                ToolCall(
                    id="c2",
                    name="terminate",
                    arguments={
                        "reason": "stub generation complete",
                        "wake_after": 0,
                    },
                )
            ],
        ),
    ]

    def complete(model, messages, tools=None):  # noqa: ANN001
        return queue.pop(0) if queue else Completion(content="(stub exhausted)")

    return complete


# --- the real body (M1 default) --------------------------------------------


def run_real_body(ctx: BodyContext) -> None:
    """The live lineage body: the real spine Agent driven as a LOOP OF TURNS.

    Carry-over files were seeded into agent/ at birth, so the prompt's "read your
    MEMORY.md, ROADMAP.md, TODO.json" resolves to real files (or, on gen-0, to their
    deliberate absence -> the prompt's gen-0 bootstrap path). Spend is metered by
    `ctx.complete`; the guardrail + continuous mirror + progress stamps ride along
    via `ctx.hooks`.

    Each turn is one `agent.run`. If the agent called terminate, return and let the
    runner perform the termination protocol. If it stopped WITHOUT terminating, this
    is the event-based idle boundary: nudge it to continue-or-terminate and loop.
    Consecutive nudges with no progress between them (no executed tool calls, an
    observed counter the agent can't fake) escalate: after `idle_nudge_limit`, end
    the generation gracefully so a fresh one is reborn. The budget acts as the other
    bound, since the watchdog only checks caps between generations.
    """
    agent = build_agent(
        ctx.config, sink=ctx.sink, hooks=ctx.hooks, complete=ctx.complete
    )
    message = _BIRTH_PROMPT
    no_progress_nudges = 0
    turn = 0
    while True:
        turn += 1
        tools_before = getattr(ctx.hooks, "tool_calls", 0)
        final = agent.run(message)

        if ctx.sink.requested:
            ctx.log(f"turn {turn}: agent requested termination ({final!r})")
            return

        if ctx.over_budget():
            ctx.log(
                f"turn {turn}: budget cap reached mid-generation; halting the lineage"
            )
            ctx.budget_halt = True
            return

        # The agent ended its turn without terminating. Did it actually do anything?
        if getattr(ctx.hooks, "tool_calls", 0) > tools_before:
            no_progress_nudges = 0
            ctx.log(f"turn {turn}: ended without terminating after work; nudging")
        else:
            no_progress_nudges += 1
            ctx.log(
                f"turn {turn}: ended without terminating and idle "
                f"(no-progress nudges {no_progress_nudges}/{ctx.config.idle_nudge_limit})"
            )
            if no_progress_nudges >= ctx.config.idle_nudge_limit:
                ctx.log(f"idle: ended after {no_progress_nudges} no-progress nudges")
                return
        message = _NUDGE_PROMPT


# --- the M0 stub bodies (offline, zero-spend; selected via KAIZEN_M0_BODY) ---


def run_stub_body(ctx: BodyContext) -> None:
    """The M0 no-op body: a real spine Agent driven by the stub provider. Proves
    the terminate tool + guardrail hook + loop end-to-end, fully offline."""
    agent = build_agent(
        ctx.config,
        sink=ctx.sink,
        hooks=ctx.hooks,
        complete=make_stub_provider(ctx.config),
    )
    final = agent.run("Make one generation of progress, then terminate.")
    ctx.log(f"stub body finished: {final!r}")


def run_broken_body(ctx: BodyContext) -> None:
    """Corrupt the body so `import spine` fails, then request termination. The
    runner must commit it, fail the boot-check, and roll back to last_good."""
    target = ctx.config.agent_src / "spine" / "__init__.py"
    target.write_text(
        target.read_text(encoding="utf-8") + "\nthis is not valid python <<<\n",
        encoding="utf-8",
    )
    ctx.log("broken body: corrupted spine/__init__.py")
    ctx.sink.requested = True
    ctx.sink.reason = "deliberately broken body (M0 rollback test)"
    ctx.sink.wake_after = 0


def run_hanging_body(ctx: BodyContext) -> None:
    """Sleep without touching the heartbeat or requesting termination, so the
    watchdog must notice the stall and reap us."""
    seconds = float(os.environ.get("KAIZEN_M0_HANG_SECONDS", "30"))
    ctx.log(f"hanging body: sleeping {seconds:g}s without heartbeat or termination")
    time.sleep(seconds)


def run_complete_body(ctx: BodyContext) -> None:
    """Declare the roadmap finished: the runner writes a HALT sentinel and the
    watchdog does not respawn. This is how the loop is allowed to win, not only
    break."""
    ctx.log("complete body: declaring roadmap complete")
    ctx.sink.requested = True
    ctx.sink.reason = "roadmap complete (M0 halt test)"
    ctx.sink.roadmap_complete = True


def run_progress_body(ctx: BodyContext) -> None:
    """A real Agent making several spaced tool calls (no LLM-side metering), then
    terminating. Proves the after_tool_call progress stamp keeps a long-but-working
    generation alive far past the backstop window (run with the liveness monitor
    effectively off, so only per-tool stamps matter)."""
    ensure_body_importable(ctx.config)
    from spine.provider import Completion, ToolCall

    steps = int(os.environ.get("KAIZEN_PROGRESS_STEPS", "6"))
    secs = os.environ.get("KAIZEN_PROGRESS_SLEEP", "0.5")
    queue = [
        Completion(
            content=None,
            tool_calls=[
                ToolCall(
                    id=f"p{i}",
                    name="bash",
                    arguments={
                        "command": f'python -c "import time; time.sleep({secs})"'
                    },
                )
            ],
        )
        for i in range(steps)
    ]
    queue.append(
        Completion(
            content="done",
            tool_calls=[
                ToolCall(
                    id="term",
                    name="terminate",
                    arguments={"reason": "progress body done", "wake_after": 0},
                )
            ],
        )
    )

    def complete(model, messages, tools=None):  # noqa: ANN001
        return queue.pop(0) if queue else Completion(content="(done)")

    agent = build_agent(ctx.config, sink=ctx.sink, hooks=ctx.hooks, complete=complete)
    agent.run("make spaced progress, then terminate")


def run_busy_child_body(ctx: BodyContext) -> None:
    """Spawn ONE long CPU-busy child and block on it (no tool calls, no LLM returns),
    then terminate. Proves process-tree observation: with no per-tool/per-LLM stamp
    in the window, only the liveness monitor seeing the active child keeps the parent
    from being reaped."""
    import subprocess

    secs = float(os.environ.get("KAIZEN_BUSY_SECONDS", "2.5"))
    code = f"import time\nt = time.time() + {secs}\nwhile time.time() < t:\n    pass"
    child = subprocess.Popen([sys.executable, "-c", code])
    ctx.log(f"busy-child body: spawned CPU child pid={child.pid}, blocking {secs:g}s")
    child.wait()
    ctx.log("busy-child body: child finished; requesting termination")
    ctx.sink.requested = True
    ctx.sink.reason = "busy child done"
    ctx.sink.wake_after = 0


_BODIES: dict[str, BodyRunner] = {
    "real": run_real_body,
    "stub": run_stub_body,
    "broken": run_broken_body,
    "hang": run_hanging_body,
    "complete": run_complete_body,
    "progress": run_progress_body,
    "busychild": run_busy_child_body,
}


def select_body_runner() -> BodyRunner:
    """Pick the body from `KAIZEN_M0_BODY` (default: the real agent). The M0
    acceptance tests set it to a stub so they prove the substrate offline; a live
    run leaves it unset and gets `run_real_body`."""
    return _BODIES.get(os.environ.get("KAIZEN_M0_BODY", "real"), run_real_body)
