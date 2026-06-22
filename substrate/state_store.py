"""substrate.state_store — continuous, atomic persistence of the lineage's state.

Invariant #4: state is flushed incrementally and at session_end, never only on
graceful exit. Every write here is atomic (temp file + os.replace) so a death at
any instant leaves a readable file, never a half-written one. A dirty death must
not erase the lineage — so the machinery that writes is deliberately simple.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from substrate.config import CARRYOVER_FILENAMES, Config


@dataclass
class Status:
    """Runtime counters the watchdog reads to enforce caps. Persisted as JSON.

    `generation` advances only when a body is blessed (committed + boot-checked),
    so it counts *successful* lives, which is what the generation cap should bound.
    """

    generation: int = 0
    budget_spent_usd: float = 0.0
    last_generation_cost_usd: float = 0.0
    consecutive_dirty: int = 0
    halted: bool = False
    halt_reason: str = ""


@dataclass
class WakeNote:
    """The body's note to the watchdog: when (or whether) to respawn."""

    halt: bool
    at: float | None  # epoch seconds for next rebirth; None = immediate
    reason: str = ""


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class StateStore:
    # Warn after this many consecutive heartbeat-stamp failures (then every Nth),
    # so a PERSISTENT write failure (read-only mount, bad perms) is loud rather than
    # masquerading as silent wedge reap-thrash.
    _HB_WARN_AFTER = 5

    def __init__(self, config: Config, *, log=None) -> None:  # noqa: ANN001
        self.config = config
        self._log = log or (lambda _msg: None)
        self._hb_fail_streak = 0

    def ensure_dirs(self) -> None:
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        self.config.journal_dir.mkdir(parents=True, exist_ok=True)

    # -- status / counters --------------------------------------------------
    def load_status(self) -> Status:
        p = self.config.status_path
        if not p.exists():
            return Status()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return Status()
        known = {f.name for f in fields(Status)}
        return Status(**{k: v for k, v in data.items() if k in known})

    def save_status(self, status: Status) -> None:
        _atomic_write(self.config.status_path, json.dumps(asdict(status), indent=2))

    # -- carry-over (memory / todo / roadmap) -------------------------------
    def flush_carryover(
        self,
        *,
        memory: str | None = None,
        todo: object | None = None,
        roadmap: str | None = None,
    ) -> None:
        if memory is not None:
            _atomic_write(self.config.memory_path, memory)
        if todo is not None:
            text = todo if isinstance(todo, str) else json.dumps(todo, indent=2)
            _atomic_write(self.config.todo_path, text)
        if roadmap is not None:
            _atomic_write(self.config.roadmap_path, roadmap)

    def read_memory(self) -> str:
        p = self.config.memory_path
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def read_roadmap(self) -> str:
        p = self.config.roadmap_path
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def read_todo(self) -> str:
        p = self.config.todo_path
        return p.read_text(encoding="utf-8") if p.exists() else ""

    # -- carry-over mirroring (state/ is canonical; agent/ is the body's copy) ---
    def seed_carryover_into_agent(self) -> list[str]:
        """Make the body's agent/ copies match canonical state, at birth.

        For each carry-over file: if state holds a non-empty copy, write it into
        agent/ so the body reads it where its prompt expects; otherwise REMOVE any
        agent/ copy, so a fresh lineage has no ROADMAP.md and the gen-0 conditional
        in the prompt fires cleanly (invariant: absence must be handled, not faked).
        Returns the names seeded (present in state).
        """
        seeded: list[str] = []
        for name in CARRYOVER_FILENAMES:
            src = self.config.state_carryover_path(name)
            dst = self.config.agent_carryover_path(name)
            if src.exists() and src.read_text(encoding="utf-8").strip():
                _atomic_write(dst, src.read_text(encoding="utf-8"))
                seeded.append(name)
            else:
                dst.unlink(missing_ok=True)
        return seeded

    def mirror_agent_to_state(self) -> list[str]:
        """Copy whatever the body has written in agent/ back to canonical state.

        Called continuously (after each tool call) and again at session_end, so a
        dirty death never erases a generation's progress (invariant #4). Only
        copies files the body actually wrote; it never deletes canonical state.
        Returns the names mirrored.
        """
        mirrored: list[str] = []
        for name in CARRYOVER_FILENAMES:
            src = self.config.agent_carryover_path(name)
            if src.exists():
                _atomic_write(
                    self.config.state_carryover_path(name),
                    src.read_text(encoding="utf-8"),
                )
                mirrored.append(name)
        return mirrored

    # -- heartbeat (the "last-progress" stamp) ------------------------------
    def touch_heartbeat(self, ts: float | None = None) -> None:
        """Stamp last-progress. Called frequently and from multiple threads (the
        per-tool/LLM stamps on the main thread, the liveness monitor on its own), so
        the temp name is unique per (pid, thread) to avoid collisions, and the whole
        thing is best-effort: a missed stamp is harmless (the next one refreshes
        liveness), and swallowing the error avoids a Windows replace-while-a-reader-
        has-it-open race turning a healthy generation into a crash. On POSIX the
        rename always succeeds, so this is simply robust."""
        path = self.config.heartbeat_path
        val = str(ts if ts is not None else time.time())
        tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(val, encoding="utf-8")
            os.replace(tmp, path)
            if self._hb_fail_streak >= self._HB_WARN_AFTER:
                self._log(
                    f"heartbeat: recovered after {self._hb_fail_streak} failed stamp(s)"
                )
            self._hb_fail_streak = 0
        except OSError as e:
            self._hb_fail_streak += 1
            # Loud on a sustained failure (a one-off collision is normal and silent).
            if self._hb_fail_streak % self._HB_WARN_AFTER == 0:
                self._log(
                    f"WARNING: heartbeat stamp has failed {self._hb_fail_streak} times "
                    f"in a row ({type(e).__name__}: {e}). Last-progress is stale — check "
                    "the mount/permissions. A persistent failure will look like the "
                    "watchdog reap-thrashing healthy generations as wedged."
                )
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def heartbeat_age(self, now: float | None = None) -> float | None:
        p = self.config.heartbeat_path
        if not p.exists():
            return None
        try:
            beat = float(p.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            return None
        return (now if now is not None else time.time()) - beat

    # -- wake / halt --------------------------------------------------------
    def write_wake(self, at: float | None, reason: str = "") -> None:
        _atomic_write(
            self.config.wake_path,
            json.dumps({"halt": False, "at": at, "reason": reason}),
        )

    def write_halt(self, reason: str = "") -> None:
        _atomic_write(
            self.config.wake_path,
            json.dumps({"halt": True, "at": None, "reason": reason}),
        )

    def read_wake(self) -> WakeNote | None:
        p = self.config.wake_path
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return WakeNote(
            halt=bool(data.get("halt")),
            at=data.get("at"),
            reason=data.get("reason", ""),
        )

    def clear_wake(self) -> None:
        self.config.wake_path.unlink(missing_ok=True)

    # -- last_good (a plain SHA file, not a git ref object) ------------------
    def read_last_good(self) -> str | None:
        p = self.config.last_good_path
        if not p.exists():
            return None
        v = p.read_text(encoding="utf-8").strip()
        return v or None

    def write_last_good(self, sha: str) -> None:
        _atomic_write(self.config.last_good_path, sha.strip() + "\n")
