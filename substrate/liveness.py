"""substrate.liveness - substrate-OBSERVED progress + process-tree custody.

Liveness is never self-reported by the agent. Two substrate-owned mechanisms feed
one "last-progress" timestamp (the heartbeat the watchdog's hard backstop reads):

  - the runner stamps progress on each observed unit of work (an LLM call returning,
    a tool call) - wired in metering and the guardrail hook, not here; and
  - `LivenessMonitor` (here) watches the agent's process SUBTREE from outside and
    stamps progress whenever a child is actively consuming CPU, so a long legitimate
    child (a build, a test run, a server) keeps the parent alive without the agent
    instrumenting anything or its processes phoning home.

It is observation, not cooperation: the agent writes whatever it likes; the
substrate watches the tree regardless. The runner also reaps leftover children at
the end of a generation (and the watchdog reaps the subtree of a wedged runner it
kills) so processes never leak across lives.

Introspection uses psutil (baked into the image; cross-platform so the same path is
exercised on the dev host and in the Linux container). Every psutil touch is guarded
- a monitoring/cleanup hiccup must never break a generation.
"""

from __future__ import annotations

import threading
from collections.abc import Callable


def _children(pid: int) -> list:
    """Recursive children of `pid`, or [] on any error (dead proc, no psutil)."""
    try:
        import psutil

        return psutil.Process(pid).children(recursive=True)
    except Exception:  # noqa: BLE001 - introspection is best-effort, never fatal
        return []


def child_subtree_active(pid: int, last_cpu: dict[int, float]) -> bool:
    """True if any child in `pid`'s subtree consumed CPU since the last sample (or is
    newly seen). Mutates `last_cpu` (child pid -> cumulative cpu seconds) in place so
    the caller can compare across polls. Newly appeared children count as active."""
    active = False
    seen: set[int] = set()
    for child in _children(pid):
        try:
            t = child.cpu_times()
            cpu = float(t.user + t.system)
        except Exception:  # noqa: S112, BLE001 - child vanished mid-poll, etc.
            continue
        seen.add(child.pid)
        prev = last_cpu.get(child.pid)
        last_cpu[child.pid] = cpu
        if prev is None or cpu > prev + 1e-6:
            active = True
    for dead in set(last_cpu) - seen:  # forget children that exited
        last_cpu.pop(dead, None)
    return active


def snapshot_children(pid: int) -> set[int]:
    """The set of child pids of `pid` right now. Captured at birth so the end-of-
    generation reap touches ONLY children spawned during the generation - never a
    pre-existing sibling (this keeps in-process tests from reaping the test runner's
    own children)."""
    return {c.pid for c in _children(pid)}


def reap_new_children(
    pid: int, baseline: set[int], *, log: Callable[[str], None] | None = None
) -> int:
    """Kill every child of `pid` that was NOT in `baseline` (i.e. spawned during the
    generation) and is still alive. Returns how many were killed. Best-effort."""
    victims = [c for c in _children(pid) if c.pid not in baseline]
    killed = 0
    for c in victims:
        try:
            c.kill()
            killed += 1
        except Exception:  # noqa: S110, BLE001
            pass
    if victims:
        try:
            import psutil

            psutil.wait_procs(victims, timeout=2)
        except Exception:  # noqa: S110, BLE001
            pass
    if killed and log:
        log(f"reaped {killed} leftover child process(es) at generation end")
    return killed


class LivenessMonitor(threading.Thread):
    """Daemon thread: while a child in the agent's subtree is actively running,
    stamp progress so the hard backstop never reaps real work. Stops on `stop()`."""

    def __init__(
        self,
        pid: int,
        on_progress: Callable[[], None],
        *,
        poll_seconds: float,
        log: Callable[[str], None] | None = None,
    ) -> None:
        # NB: `_stop` collides with a threading.Thread internal — use a private name.
        super().__init__(daemon=True, name="kaizen-liveness")
        self._pid = pid
        self._on_progress = on_progress
        self._poll = max(0.05, poll_seconds)
        self._log = log
        self._cancel = threading.Event()
        self._last_cpu: dict[int, float] = {}

    def run(self) -> None:
        while not self._cancel.wait(self._poll):
            try:
                if child_subtree_active(self._pid, self._last_cpu):
                    self._on_progress()
            except Exception:  # noqa: S110, BLE001 - never let monitoring crash the run
                pass

    def stop(self) -> None:
        self._cancel.set()
