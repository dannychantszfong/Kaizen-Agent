"""runner.py — one generation's lifecycle. The womb, not the agent.

Lives in the substrate (outside agent/), so the body can never rewrite it. The
runner loads the lineage's state, brings a body to life in-process (one process,
one PID — "termination" just means this process exits), and, when the body
*requests* termination, performs the ordered protocol the body cannot reach:

    1. persist state first        (a death past here is not amnesiac)
    2. commit agent/  -> C        (the candidate becomes a real ref)
    3. boot-check a clean         (validate exactly what rebirth will check out —
       checkout of C               a temp git worktree, not the dirty tree)
    4. bless last_good = C        (only a booted body becomes the rollback target)
    5. leave a wake / halt note   (scheduling authority stays in the substrate)
    6. exit                       (the watchdog takes it from here)

If the boot-check fails the death is aborted: the candidate is left unblessed and
the working tree is reset to last_good, so a broken body never ends the lineage.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from enum import IntEnum
from pathlib import Path

# Put the editable body on the path before anything imports `spine` — the
# substrate's guardrail subclasses spine's Hooks at import time. Runner and body
# both live at <root>/, with the body at <root>/agent/src.
sys.path.insert(0, str(Path(__file__).resolve().parent / "agent" / "src"))

from substrate import body as body_mod  # noqa: E402
from substrate import liveness  # noqa: E402
from substrate.config import Config  # noqa: E402
from substrate.guardrail import SubstrateHooks  # noqa: E402
from substrate.metering import CostMeter, make_metered_complete  # noqa: E402
from substrate.state_store import StateStore, Status  # noqa: E402


class Exit(IntEnum):
    """Process exit codes the watchdog reads. Scheduling intent itself travels in
    the wake/halt note, not the code (invariant #6)."""

    GRACEFUL = 0
    DIRTY = 1
    ROLLED_BACK = 2


class GitError(RuntimeError):
    pass


def _git(config: Config, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", str(config.root), *args],
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
        )
    return proc


class Runner:
    def __init__(
        self,
        config: Config,
        *,
        body_runner=None,  # noqa: ANN001
        provider_factory=None,  # noqa: ANN001 - (config, meter) -> complete()
        log=None,  # noqa: ANN001
    ) -> None:
        self.config = config
        self.store = StateStore(config, log=self.log)
        self.body_runner = body_runner or body_mod.select_body_runner()
        # None => the runner builds the real metered complete itself (with cap +
        # prior spend + logger for the live per-call line). Tests inject a factory.
        self._provider_factory = provider_factory
        self._extra_log = log
        self._lines: list[str] = []

    # -- logging / journal --------------------------------------------------
    def log(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        self._lines.append(line)
        if self._extra_log:
            self._extra_log(line)

    def _flush_journal(self, generation: int) -> None:
        path = self.config.journal_path(generation)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write("\n".join(self._lines) + "\n")
        self._lines.clear()

    # -- entry point --------------------------------------------------------
    def run_one_generation(self) -> int:
        self.store.ensure_dirs()
        status = self.store.load_status()
        gen = status.generation
        # Children present BEFORE the body runs are not ours to reap (in-process
        # tests share a pid with their test runner); only kill children the
        # generation itself spawned, so nothing leaks across lives.
        baseline = liveness.snapshot_children(os.getpid())
        try:
            return self._run(status, gen)
        finally:
            liveness.reap_new_children(os.getpid(), baseline, log=self.log)
            self._flush_journal(gen)

    def _run(self, status: Status, gen: int) -> int:
        self.log(f"=== generation {gen} birth ===")
        self._birth()
        # Fold the lineage's carry-over (MEMORY/ROADMAP/TODO) into agent/ so the
        # body reads it where its prompt expects. On gen-0 state is empty and this
        # leaves agent/ without a ROADMAP, which is what triggers bootstrap.
        seeded = self.store.seed_carryover_into_agent()
        self.log(f"birth: seeded carry-over {seeded or '(none — generation zero)'}")
        self.store.touch_heartbeat()

        body_mod.ensure_body_importable(self.config)
        from spine.tools.terminate import TerminationRequest

        sink = TerminationRequest()
        meter = CostMeter()
        if self._provider_factory is not None:
            complete = self._provider_factory(self.config, meter)
        else:
            # Provider-agnostic: price whatever model the body chose (agent/MODEL or
            # the default), summed into the one lineage total against the one cap.
            # on_progress stamps last-progress on each LLM return (substrate-owned).
            complete = make_metered_complete(
                meter,
                model=body_mod.resolve_model(self.config),
                cap_usd=self.config.budget_cap_usd,
                prior_spent_usd=status.budget_spent_usd,
                log=self.log,
                on_progress=self.store.touch_heartbeat,
            )
        hooks = SubstrateHooks(self.config, self.store, log=self.log)
        ctx = body_mod.BodyContext(
            config=self.config,
            sink=sink,
            hooks=hooks,
            log=self.log,
            generation=gen,
            complete=complete,
            over_budget=lambda: (status.budget_spent_usd + meter.total_usd)
            >= self.config.budget_cap_usd,
        )

        # Background liveness monitor: stamps last-progress while a child in this
        # process's subtree is actively running, so a long legitimate child does not
        # let the hard backstop reap real work. Substrate-owned; the agent can't fake
        # or disable it.
        monitor = liveness.LivenessMonitor(
            os.getpid(),
            self.store.touch_heartbeat,
            poll_seconds=self.config.liveness_poll_seconds,
            log=self.log,
        )
        monitor.start()

        # Run the body with the working directory inside agent/, so its `bash`
        # relative paths resolve under the body — matching the guardrail's
        # assumption that only `..`/absolute paths can escape (invariant #5).
        crashed = False
        prev_cwd = os.getcwd()
        try:
            os.chdir(self.config.agent_dir)
            self.body_runner(ctx)
        except BaseException as e:  # noqa: BLE001 - a crashing body is a dirty death
            self.log(f"body crashed: {type(e).__name__}: {e}")
            crashed = True
        finally:
            os.chdir(prev_cwd)
            monitor.stop()
            monitor.join(timeout=2)

        # Always, on every exit path: capture what the body wrote (invariant #4)
        # and charge what it really spent — a body that burned tokens then crashed
        # still cost money, and the budget cap must see it.
        self.store.mirror_agent_to_state()
        self._charge(status, meter, gen)

        # Circuit breaker: if any call ran a model the meter could not price, the
        # budget cap is blind. HALT the lineage rather than fly blind (don't bless,
        # don't respawn) — the watchdog reads both the halt flag and the sentinel.
        if meter.had_unpriceable:
            reason = (
                "unpriceable model(s) "
                + ", ".join(meter.unpriceable_models)
                + " — the meter went blind; halting the lineage (circuit breaker)"
            )
            self.log(reason)
            self.store.write_halt(reason)
            status.halted = True
            status.halt_reason = "unpriceable_model"
            self._restore_agent_to_last_good()
            return self._finish_dirty(status, reason, rolled_back=True)

        # Intra-generation budget fuse: the turn loop hit the cap mid-life. HALT the
        # lineage explicitly (sentinel + flag) rather than ending into a respawn the
        # watchdog would have to re-catch. (The cumulative between-generation budget
        # cap stays the watchdog's job; this is the within-one-generation bound.)
        if ctx.budget_halt:
            reason = (
                f"budget cap reached mid-generation "
                f"(${status.budget_spent_usd:.4f} >= "
                f"${self.config.budget_cap_usd:.2f}); halting the lineage"
            )
            self.log(reason)
            self.store.write_halt(reason)
            status.halted = True
            status.halt_reason = "budget"
            self._restore_agent_to_last_good()
            return self._finish_dirty(status, reason, rolled_back=True)

        if crashed:
            self._restore_agent_to_last_good()
            return self._finish_dirty(status, "body crashed", rolled_back=True)

        if not sink.requested:
            # The body ended a generation without asking to terminate — an idle
            # escalation, a budget-bounded end, or a body that just returned. Treat
            # it as a controlled (not crashed) end: drop the unblessed partial edits,
            # keep the continuously-mirrored memory, and let the watchdog respawn a
            # fresh generation (its crash-loop cap catches a lineage that only idles).
            self.log("body ended without requesting termination; respawning fresh")
            self._restore_agent_to_last_good()
            return self._finish_dirty(
                status, "no termination request", rolled_back=True
            )

        return self._terminate(status, sink)

    # -- birth: start from the last blessed, committed body -----------------
    def _birth(self) -> None:
        last_good = self.store.read_last_good()
        if last_good:
            self.log(f"birth: checking out last_good {last_good[:10]}")
            _git(self.config, "checkout", "--force", last_good, "--", "agent")
        else:
            self.log("birth: no last_good yet (generation 0)")

    # -- the termination protocol -------------------------------------------
    def _terminate(self, status: Status, sink) -> int:  # noqa: ANN001
        # 1. persist state first.
        self.store.save_status(status)
        self.log("protocol 1/5: state persisted")

        # 2. commit the candidate body.
        sha = self._commit_candidate(status.generation, sink.reason)
        self.log(f"protocol 2/5: committed candidate {sha[:10]}")

        # 3. boot-check a clean checkout of the committed ref.
        ok, detail = self._boot_check(sha)
        if not ok:
            self.log(f"protocol 3/5: BOOT-CHECK FAILED: {detail}")
            self._rollback()
            self.log("death aborted; rolled back to last_good")
            return self._finish_dirty(status, "boot-check failed", rolled_back=True)
        self.log(f"protocol 3/5: boot-check passed ({detail})")

        # 4. bless last_good = C (only now is the candidate a known-good target).
        self.store.write_last_good(sha)
        self.log(f"protocol 4/5: blessed last_good = {sha[:10]}")

        # 5. wake or halt.
        if sink.roadmap_complete:
            self.store.write_halt(sink.reason)
            self.log("protocol 5/5: roadmap complete -> HALT sentinel written")
        else:
            at = time.time() + (sink.wake_after or 0.0)
            self.store.write_wake(at, sink.reason)
            self.log(
                f"protocol 5/5: wake note written (rebirth in {sink.wake_after or 0:g}s)"
            )

        # advance counters: this life was blessed. (Spend was already charged in
        # _charge, on every exit path, so a dirty death is billed too.)
        status.generation += 1
        status.consecutive_dirty = 0
        self.store.save_status(status)
        self.log(
            f"generation {status.generation - 1} graceful; next is {status.generation}"
        )
        return int(Exit.GRACEFUL)

    def _finish_dirty(
        self, status: Status, reason: str, rolled_back: bool = False
    ) -> int:
        status.consecutive_dirty += 1
        self.store.save_status(status)
        self.log(
            f"generation ended DIRTY: {reason} "
            f"(consecutive_dirty={status.consecutive_dirty})"
        )
        return int(Exit.ROLLED_BACK if rolled_back else Exit.DIRTY)

    # -- cost: metered real spend, surfaced live and persisted for the watchdog --
    def _charge(self, status: Status, meter: CostMeter, gen: int) -> None:
        """Add this generation's spend to the lineage total and persist it before
        anything else, so the watchdog's budget cap reads it even on a dirty death.
        `stub_cost_per_generation_usd` keeps the cap exercisable offline (it is 0
        for a live run; the meter carries the real dollars there)."""
        gen_cost = meter.total_usd + self.config.stub_cost_per_generation_usd
        status.budget_spent_usd += gen_cost
        status.last_generation_cost_usd = gen_cost
        self.store.save_status(status)
        self.log(
            f"gen {gen} cost ${gen_cost:.4f} "
            f"({meter.calls} call(s), {meter.total_tokens} tokens); "
            f"lineage total ${status.budget_spent_usd:.4f} "
            f"of ${self.config.budget_cap_usd:.2f} cap"
        )

    def _restore_agent_to_last_good(self) -> None:
        """Discard a dirty generation's uncommitted edits: reset agent/ to the last
        blessed body and drop untracked junk it left. Canonical MEMORY/TODO/ROADMAP
        were already mirrored to state, and are re-seeded into agent/ at next birth,
        so this loses code-in-progress only — never the lineage's memory."""
        last_good = self.store.read_last_good()
        if last_good:
            _git(self.config, "checkout", "--force", last_good, "--", "agent")
        _git(self.config, "clean", "-fd", "--", "agent", check=False)
        self.log(
            "restored agent/ to last_good (discarded the dirty generation's edits)"
        )

    # -- git plumbing -------------------------------------------------------
    def _commit_candidate(self, gen: int, reason: str) -> str:
        _git(self.config, "add", "--", "agent")
        msg = f"gen {gen}: {reason}".strip()
        _git(self.config, "commit", "--allow-empty", "-m", msg)
        return _git(self.config, "rev-parse", "HEAD").stdout.strip()

    def _boot_check(self, sha: str) -> tuple[bool, str]:
        cfg = self.config
        tmp = Path(tempfile.mkdtemp(prefix="kaizen-bootcheck-"))
        worktree = tmp / "wt"
        try:
            _git(cfg, "worktree", "add", "--detach", str(worktree), sha)
            agent_src = worktree / "agent" / "src"
            proc = subprocess.run(
                [sys.executable, str(cfg.bootcheck_script), str(agent_src)],
                capture_output=True,
                text=True,
                timeout=cfg.boot_check_timeout_seconds,
            )
            detail = (proc.stdout + proc.stderr).strip().replace("\n", " | ")
            return proc.returncode == 0, detail
        except subprocess.TimeoutExpired:
            return (
                False,
                f"boot-check timed out after {cfg.boot_check_timeout_seconds}s",
            )
        finally:
            _git(cfg, "worktree", "remove", "--force", str(worktree), check=False)
            shutil.rmtree(tmp, ignore_errors=True)

    def _rollback(self) -> None:
        """Reset the working tree to last_good. The unblessed candidate commit is
        left dangling (recoverable via reflog) for forensics, off the branch tip."""
        last_good = self.store.read_last_good()
        if last_good:
            _git(self.config, "reset", "--hard", last_good)
        else:
            # No last_good yet: drop the candidate we just committed.
            _git(self.config, "reset", "--hard", "HEAD~1", check=False)


def _build_config(argv: argparse.Namespace) -> Config:
    config = Config.load(argv.root)
    if argv.body_runner:
        # tests/observability may want to name a specific stub; env is the default.
        pass
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one Kaizen generation.")
    parser.add_argument("--root", required=True, help="Repo root (the substrate).")
    parser.add_argument(
        "--body-runner",
        default=None,
        help="(reserved) override body selection; default reads KAIZEN_M0_BODY.",
    )
    args = parser.parse_args(argv)
    config = _build_config(args)
    runner = Runner(config, log=lambda line: print(line, flush=True))
    return runner.run_one_generation()


if __name__ == "__main__":
    sys.exit(main())
