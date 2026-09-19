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
    schedulers (crontab, systemctl, schtasks, ...), writing outside agent/; read-only commands and standard output sinks
    are allowed. Unknown shell commands receive conservative path screening.
  - `write` / `edit`: a target path that resolves outside agent/. The OS makes the
    immortal *code* read-only, but the runtime *state* (substrate/state, journal,
    .git) is agent-writable, so without this a path like `../substrate/state/last_good`
    would let the body corrupt its own lineage state. This closes that path the way
    the bash guard closes the shell.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

from spine.hooks import BeforeToolCall, Hooks
from spine.tools.base import Tool, ToolResult

from substrate.metering import clip_lines, redact

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

# Known read-only shell operations may read paths anywhere. Unknown commands
# retain conservative path screening: this is a guard against accidents, not a
# shell sandbox. The container remains the security boundary.
_READ_ONLY = frozenset(
    {
        "cat",
        "head",
        "tail",
        "ls",
        "pwd",
        "wc",
        "stat",
        "file",
        "diff",
        "cmp",
        "readlink",
    }
)
_SINKS = frozenset(
    {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/fd/1", "/dev/fd/2"}
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

    # Tokenize shell punctuation so each pipeline/command and every redirect is
    # checked independently. Do not grant a read exemption to command substitution.
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
        lexer.whitespace_split = True
        lexer.escape = ""  # preserve Windows path separators in offline tests
        tokens = list(lexer)
    except ValueError:
        return "Blocked: cannot safely inspect malformed shell quoting."

    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in {";", "&&", "||", "|", "&", "(", ")"}:
            segments.append([])
        else:
            segments[-1].append(token)
    for segment in segments:
        operands: list[str] = []
        i = 0
        while i < len(segment):
            token = segment[i]
            if ">" in token and set(token) <= set("<>&|"):
                i += 1
                if i >= len(segment):
                    return "Blocked: missing redirection target."
                target = segment[i]
                if target not in _SINKS and not (
                    "&" in token and (target.isdigit() or target == "-")
                ):
                    reason = inspect_path(target, agent_dir=agent_dir)
                    if reason:
                        return reason
            elif token == "<":
                i += 1  # input redirection reads; filesystem permissions apply
            else:
                operands.append(token)
            i += 1
        if not operands:
            continue
        binary = operands[0]
        readonly = binary in _READ_ONLY
        if binary in {"grep", "rg"}:
            readonly = not any(x.startswith("--pre") for x in operands[1:])
        if readonly and not any(x in command for x in ("$(", "`", "\n")):
            continue
        for token in operands:
            if Path(token).is_absolute():
                # Preserve quoted paths containing spaces as one real path.
                if token in _SINKS:
                    continue
                reason = inspect_path(token, agent_dir=agent_dir)
                if reason:
                    return reason
                continue
            # Include paths embedded in simple interpreter code / options, as the
            # old guard did; do not confuse test_runner.py with ../runner.py.
            for path in re.split(r"[\s='\"(),]+", token):
                if path in _SINKS:
                    continue
                if (
                    path.startswith(("/", "..", "\\"))
                    or ".." in path.replace("\\", "/").split("/")
                    or re.match(r"^[A-Za-z]:[\\/]", path)
                ):
                    reason = inspect_path(path, agent_dir=agent_dir)
                    if reason:
                        return reason

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

    candidate = Path(path.replace("\\", "/"))
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

    def __init__(self, config, store, *, log=None) -> None:
        self.config = config
        self.store = store
        self._log = log or (lambda _msg: None)
        # Count of executed tool calls this generation. Substrate-observed progress:
        # the runner reads it across a turn to tell a working turn from an idle one
        # (the idle-nudge escalation). The agent cannot touch this.
        self.tool_calls = 0

    def before_tool_call(self, tool: Tool, args, agent) -> BeforeToolCall:
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
    ) -> ToolResult:
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
        if getattr(self.config, "log_transcript", False):
            try:
                flag = "ERR" if result.is_error else "ok"
                limit = getattr(self.config, "transcript_max_chars", 0)
                body = clip_lines(redact(result.output), limit)
                if "\n" in body:
                    block = "\n".join("      " + ln for ln in body.splitlines())
                    self._log(f"  · {tool.name} → {flag}:\n{block}")
                else:
                    self._log(f"  · {tool.name} → {flag}: {body}")
            except Exception:  # noqa: S110, BLE001 - observability never breaks a tool call
                pass
        if tool.name in _MUTATING_TOOLS:
            mirrored = self.store.mirror_agent_to_state()
            if mirrored:
                self._log(f"checkpoint: mirrored {', '.join(mirrored)} to state")
        return result

    def session_end(self, agent) -> None:
        """Final backstop mirror. Fires from `Agent.run`'s `finally`, so it runs
        even when a turn ends by exception — but the per-tool mirror above means
        the lineage is already current; this just catches a last unsynced write.
        """
        mirrored = self.store.mirror_agent_to_state()
        if mirrored:
            self._log(f"session_end: mirrored {', '.join(mirrored)} to state")
