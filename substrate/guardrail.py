"""substrate.guardrail — the agent's brakes, held where the agent can't reach.

This is invariant #5 made real. spine ships `Hooks` permissive, and its base lives
*inside* the agent's writable tree (`agent/src/spine/hooks.py`), so the agent could
edit that. Therefore the real policy lives *here*, in the substrate, and the runner
injects it when it builds the body. The agent can rewrite the permissive base all
it likes; the runner overrides it at construction with a class the agent cannot
write to.

Honest scope: this is the in-process first line, working on the `bash` command
string and on the `write`/`edit` target path, and it is best-effort. A determined
body that rewrites its own loop to skip hooks can get around it. The *hard* wall is
OS isolation (invariant #1) plus the watchdog being a separate process (#2): even a
hook-skipping body is boxed by the sandbox and cannot reach its supervisor. This hook
makes the common, accidental escapes loud and recoverable; the sandbox makes the rest
impossible.

What it blocks (invariants #5/#6):
  - `bash`: sending signals / reaping processes (kill, taskkill, ...), editing
    schedulers (crontab, systemctl, schtasks, ...), naming the immortal layer
    (substrate/, watchdog.py, runner.py), or escaping agent/ via `..` / an absolute
    path outside agent/.
  - `write` / `edit`: a target path that resolves outside agent/. The OS makes the
    immortal *code* read-only, but the runtime *state* (substrate/state, journal,
    .git) is agent-writable, so without this a path like `../substrate/state/last_good`
    would let the body corrupt its own lineage state. This closes that path the way
    the bash guard closes the shell.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from spine.hooks import BeforeToolCall, Hooks
from spine.tools.base import Tool, ToolResult

# Tools that can change a carry-over file and so warrant a re-mirror to state.
_MUTATING_TOOLS = frozenset({"write", "edit", "bash"})

# Tools whose target path must stay inside agent/ (they write the filesystem
# directly, so the bash command guard doesn't see them).
_PATH_TOOLS = frozenset({"write", "edit"})

# Binaries that hand the agent process-lifecycle or scheduling power. Authority
# over both belongs to the substrate, never the body.
_DENY_BINARIES = (
    "kill",
    "pkill",
    "killall",
    "taskkill",
    "crontab",
    "systemctl",
    "systemd-run",
    "schtasks",
    "shutdown",
    "reboot",
)

# Names of the immortal layer. The body's writable world is agent/ only.
_BLOCKED_REFERENCES = ("substrate", "watchdog.py", "runner.py")

# A `..` segment that escapes the current directory.
_PARENT_ESCAPE = re.compile(r"(?:^|[\s=:'\"(])\.\.(?:[\\/]|$)")

# Absolute-looking path tokens: a Windows drive path (C:\ or C:/) or a POSIX
# absolute path (/etc/...). The leading separator class avoids matching URL
# schemes like https:// (the `/` there is preceded by ':') and `//`.
_ABS_TOKEN = re.compile(
    r"""(?:^|[\s'"=(])
        (
          [A-Za-z]:[\\/][^\s'";|&()<>]*
          |
          /[^\s'";|&()<>/][^\s'";|&()<>]*
        )
    """,
    re.VERBOSE,
)


def _within(token: str, base: Path) -> bool:
    """Is an absolute `token` inside `base`? Lexical, no symlink resolution (the
    target may not exist yet). Case-insensitive on Windows."""
    t = os.path.normpath(token)
    b = os.path.normpath(str(base))
    if os.name == "nt":
        t, b = t.lower(), b.lower()
    return t == b or t.startswith(b + os.sep)


def inspect_bash_command(command: str, *, agent_dir: Path) -> str | None:
    """Return a block reason if `command` reaches outside the body, else None.

    Pure and standalone so it is cheap to unit-test against representative
    escapes. Best-effort by design (see module docstring).
    """
    lowered = command.lower()

    for binary in _DENY_BINARIES:
        if re.search(rf"(?<![\w-]){re.escape(binary)}(?![\w-])", lowered):
            return (
                f"Blocked: '{binary}' is off-limits. Process lifecycle and "
                "scheduling belong to the substrate, not the agent (invariants "
                "#5/#6). Leave a wake note via `terminate` instead."
            )

    for ref in _BLOCKED_REFERENCES:
        if ref in lowered:
            return (
                f"Blocked: '{ref}' is part of the substrate — the immortal layer. "
                "Your only writable tree is agent/ (invariants #2/#5)."
            )

    if _PARENT_ESCAPE.search(command):
        return (
            "Blocked: a '..' path escape. You may only touch paths under agent/ "
            "(invariant #5)."
        )

    for m in _ABS_TOKEN.finditer(command):
        token = m.group(1)
        if not _within(token, agent_dir):
            return (
                f"Blocked: absolute path '{token}' is outside agent/. Your only "
                "writable tree is agent/ (invariant #5)."
            )

    return None


def inspect_path(path: str, *, agent_dir: Path) -> str | None:
    """Return a block reason if writing/editing `path` reaches outside the body's
    tree, else None. The body's cwd is agent/, so a bare relative path is fine; the
    escapes are naming the immortal layer, or a path (absolute, or a `..` chain) that
    resolves outside agent/. Resolved against `agent_dir` explicitly rather than the
    live cwd, and lexically (no symlink resolution — a target may not exist yet).

    Unlike the bash guard this can resolve the exact path, so an in-tree `..`
    (e.g. `src/../MEMORY.md`) is correctly allowed; only a `..` that leaves agent/ is
    blocked. Pure and standalone so it is cheap to unit-test.
    """
    if not isinstance(path, str) or not path.strip():
        return None  # let the tool's own validation handle an empty/odd path

    lowered = path.replace("\\", "/").lower()
    for ref in _BLOCKED_REFERENCES:
        if ref in lowered:
            return (
                f"Blocked: '{ref}' is part of the substrate — the immortal layer. "
                "Your only writable tree is agent/ (invariants #2/#5)."
            )

    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = agent_dir / candidate
    if not _within(str(candidate), agent_dir):
        return (
            f"Blocked: path '{path}' resolves outside agent/. Your only writable "
            "tree is agent/ (invariant #5)."
        )

    return None


class SubstrateHooks(Hooks):
    """The injected policy: the brake on `bash`, plus the session_end state flush.

    Both responsibilities are substrate-owned (the agent can't edit this class),
    which is why they live together here rather than in the body.
    """

    def __init__(self, config, store, *, log=None) -> None:  # noqa: ANN001
        self.config = config
        self.store = store
        self._log = log or (lambda _msg: None)
        # Count of executed tool calls this generation. Substrate-observed progress:
        # the runner reads it across a turn to tell a working turn from an idle one
        # (the idle-nudge escalation). The agent cannot touch this.
        self.tool_calls = 0

    def before_tool_call(self, tool: Tool, args, agent) -> BeforeToolCall:  # noqa: ANN001
        if tool.name == "bash":
            reason = inspect_bash_command(args.command, agent_dir=self.config.agent_dir)
            if reason:
                self._log(f"GUARDRAIL blocked bash {args.command!r}: {reason}")
                return BeforeToolCall(blocked=True, message=reason)
        elif tool.name in _PATH_TOOLS:
            reason = inspect_path(args.path, agent_dir=self.config.agent_dir)
            if reason:
                self._log(f"GUARDRAIL blocked {tool.name} {args.path!r}: {reason}")
                return BeforeToolCall(blocked=True, message=reason)
        return BeforeToolCall()

    def after_tool_call(
        self, tool: Tool, args, result: ToolResult, agent
    ) -> ToolResult:  # noqa: ANN001
        """Fires after a tool actually executes (blocked tools never reach here).

        Two substrate-owned jobs, plus invariant #4. An executed tool call is an
        observed unit of work, so we (1) count it for the idle-nudge no-progress
        check and (2) stamp last-progress for the hard backstop. Then, invariant #4:
        after any tool that could have changed a carry-over file, mirror agent/ ->
        canonical state, so a dirty death between tool calls still leaves the latest
        MEMORY/TODO/ROADMAP on the durable volume. Never alters the result.
        """
        self.tool_calls += 1
        self.store.touch_heartbeat()
        if tool.name in _MUTATING_TOOLS:
            mirrored = self.store.mirror_agent_to_state()
            if mirrored:
                self._log(f"checkpoint: mirrored {', '.join(mirrored)} to state")
        return result

    def session_end(self, agent) -> None:  # noqa: ANN001
        """Final backstop mirror. Fires from `Agent.run`'s `finally`, so it runs
        even when a turn ends by exception — but the per-tool mirror above means
        the lineage is already current; this just catches a last unsynced write.
        """
        mirrored = self.store.mirror_agent_to_state()
        if mirrored:
            self._log(f"session_end: mirrored {', '.join(mirrored)} to state")
