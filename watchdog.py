"""watchdog.py — the immortal supervisor. A separate process, outside agent/.

It is the only thing that may spawn a generation, kill a hung one, count
generations and dollars, and decide rebirth vs halt. The body can neither read,
write, nor signal it (invariants #2, #5, #6). It is deliberately dumb and boring:
a loop that enforces caps, honors the body's wake/halt note, spawns the runner as a
subprocess it owns the PID of, watches the heartbeat, and reaps anything that hangs.

Caps and the crash-loop guard are the watchdog's own judgment, tracked from what it
directly observes (did the generation counter advance?), so they hold even when a
runner is hard-killed before it can record anything.
"""

from __future__ import annotations

import argparse
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

# Put the editable body on the path before importing the substrate package, whose
# __init__ pulls in the guardrail (which subclasses spine's Hooks at import time).
sys.path.insert(0, str(Path(__file__).resolve().parent / "agent" / "src"))

from substrate import liveness  # noqa: E402
from substrate.config import Config  # noqa: E402
from substrate.state_store import StateStore  # noqa: E402


class Watchdog:
    def __init__(
        self,
        config: Config,
        *,
        spawn=None,  # noqa: ANN001 - () -> subprocess.Popen-like
        log=None,  # noqa: ANN001
        max_total_spawns: int | None = None,
    ) -> None:
        self.config = config
        self._log = log or (lambda msg: print(msg, flush=True))
        self.store = StateStore(config, log=self.log)
        self._spawn = spawn or self._default_spawn
        self.max_total_spawns = max_total_spawns

        self.spawns = 0
        self.consecutive_no_progress = 0
        self._last_spawn_at: float | None = None

    def log(self, msg: str) -> None:
        self._log(f"[watchdog] {msg}")

    def _default_spawn(self):
        # Optionally drop privilege before running the runner. In the container the
        # watchdog runs as root and KAIZEN_RUNNER_PREFIX="gosu agent" runs the
        # runner + its in-process body as the unprivileged `agent` uid; on a dev
        # host the prefix is empty and the runner runs as the current user. The
        # prefix is the substrate's choice, never the agent's (invariant #6).
        prefix = shlex.split(os.environ.get("KAIZEN_RUNNER_PREFIX", ""))
        cmd = [
            *prefix,
            sys.executable,
            str(self.config.runner_script),
            "--root",
            str(self.config.root),
        ]
        # start_new_session: on POSIX the runner leads its own process group/session,
        # so a stale-heartbeat reap can kill the WHOLE group (catching reparented `&`
        # children); a no-op on Windows. Cleanup falls back to a psutil subtree walk.
        return subprocess.Popen(cmd, start_new_session=True)

    # -- startup ------------------------------------------------------------
    def bootstrap(self) -> None:
        """Make the substrate ready and guarantee a last_good exists before the
        first generation, so even a gen-0 break has a rollback target."""
        self.store.ensure_dirs()
        self.config.save()
        if self.store.read_last_good() is None:
            head = subprocess.run(
                ["git", "-C", str(self.config.root), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
            )
            if head.returncode == 0 and head.stdout.strip():
                self.store.write_last_good(head.stdout.strip())
                self.log(f"bootstrapped last_good = {head.stdout.strip()[:10]}")
            else:
                self.log("WARNING: no git HEAD to bootstrap last_good from")

    # -- the supervisor loop ------------------------------------------------
    def run(self) -> str:
        """Run until a cap halts the lineage or the safety bound is hit. Returns
        the halt reason."""
        self.bootstrap()
        while True:
            halt = self._check_caps()
            if halt:
                self.log(f"halting: {halt}")
                return halt

            if (
                self.max_total_spawns is not None
                and self.spawns >= self.max_total_spawns
            ):
                self.log("reached max_total_spawns (safety bound)")
                return "max_total_spawns"

            delay = self._respawn_delay()
            if delay > 0:
                time.sleep(delay)

            self._spawn_and_supervise()

    def _check_caps(self) -> str | None:
        wake = self.store.read_wake()
        if wake and wake.halt:
            return f"halt sentinel ({wake.reason})"

        status = self.store.load_status()
        if status.halted:
            return f"status halted ({status.halt_reason})"
        if status.generation >= self.config.max_generations:
            self._mark_halted("max_generations")
            return "generation cap reached"
        if status.budget_spent_usd >= self.config.budget_cap_usd:
            self._mark_halted("budget")
            return "budget cap reached"
        if self.consecutive_no_progress >= self.config.crash_loop_threshold:
            self._mark_halted("crash_loop")
            return "crash-loop threshold reached"
        return None

    def _mark_halted(self, reason: str) -> None:
        status = self.store.load_status()
        status.halted = True
        status.halt_reason = reason
        self.store.save_status(status)

    def _respawn_delay(self) -> float:
        now = time.time()
        target = now
        wake = self.store.read_wake()
        if wake and not wake.halt and wake.at:
            target = max(target, wake.at)
        if self._last_spawn_at is not None:
            target = max(target, self._last_spawn_at + self.config.min_respawn_seconds)
        return max(0.0, target - now)

    def _spawn_and_supervise(self) -> None:
        before = self.store.load_status().generation
        # Fresh heartbeat at spawn so a slow start isn't mistaken for a hang.
        self.store.touch_heartbeat()

        proc = self._spawn()
        self.spawns += 1
        self._last_spawn_at = time.time()
        self.log(
            f"spawned runner pid={getattr(proc, 'pid', '?')} (spawn #{self.spawns})"
        )

        killed = self._supervise(proc)
        # Whether it exited or was reaped, sweep any descendants it left so nothing
        # leaks across generations (the runner cleans its own subtree on a graceful
        # end; this also catches a wedged runner's children and reparented orphans).
        self._sweep(proc)

        after = self.store.load_status().generation
        if after > before:
            self.consecutive_no_progress = 0
            self.log(f"generation advanced {before} -> {after}")
        else:
            self.consecutive_no_progress += 1
            how = "killed (stale heartbeat)" if killed else "exited without progress"
            self.log(
                f"no progress: runner {how} "
                f"(consecutive_no_progress={self.consecutive_no_progress})"
            )

    def _supervise(self, proc) -> bool:  # noqa: ANN001
        """Watch one runner until it exits or hangs. Returns True if we killed it."""
        while True:
            if proc.poll() is not None:
                self.log(
                    f"runner pid={getattr(proc, 'pid', '?')} exited code={proc.returncode}"
                )
                return False
            age = self.store.heartbeat_age()
            if age is not None and age > self.config.heartbeat_timeout_seconds:
                self.log(
                    f"heartbeat stale ({age:.1f}s > "
                    f"{self.config.heartbeat_timeout_seconds}s); reaping wedged "
                    f"runner pid={getattr(proc, 'pid', '?')}"
                )
                # Kill the agent's children FIRST (while still parented to the live
                # runner so they're findable), then the runner itself.
                self._kill_subtree(proc)
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    pass
                return True
            time.sleep(self.config.supervise_poll_seconds)

    def _kill_subtree(self, proc) -> None:  # noqa: ANN001
        """Kill the runner's children (the agent's process subtree). The runner
        process itself is killed by the caller."""
        pid = getattr(proc, "pid", None)
        if pid is None:
            return
        try:
            liveness.reap_new_children(pid, set(), log=self.log)
        except Exception:  # noqa: BLE001 - cleanup is best-effort, never fatal
            pass

    def _sweep(self, proc) -> None:  # noqa: ANN001
        """After a generation ends, sweep leftover descendants so none leak across
        lives. On POSIX, kill the runner's process group (it led its own session via
        start_new_session, so reparented `&` children share its pgid). Plus a
        best-effort subtree walk for any still parented to a live runner."""
        pid = getattr(proc, "pid", None)
        if pid is None:
            return
        killpg = getattr(os, "killpg", None)
        if killpg is not None:
            try:
                killpg(pid, signal.SIGKILL)  # pgid == pid (session leader)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        self._kill_subtree(proc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Supervise a Kaizen lineage.")
    parser.add_argument("--root", required=True, help="Repo root (the substrate).")
    parser.add_argument("--max-generations", type=int, default=None)
    parser.add_argument("--budget-cap", type=float, default=None)
    args = parser.parse_args(argv)

    overrides = {}
    if args.max_generations is not None:
        overrides["max_generations"] = args.max_generations
    if args.budget_cap is not None:
        overrides["budget_cap_usd"] = args.budget_cap
    config = Config(root=Path(args.root), **overrides)

    watchdog = Watchdog(config)
    reason = watchdog.run()
    print(f"[watchdog] stopped: {reason}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
