"""M1 liveness acceptance checks - the layered liveness redesign, proven offline.

Each maps to a piece of the model:

  - progress past the old window is NOT reaped     (test_progress_keeps_generation_alive)
  - a long-running child keeps the parent alive     (test_busy_child_keeps_parent_alive)
  - a true wedge (no progress, no child) IS reaped   (test_wedged_generation_reaped)
  - stop-without-terminate is nudged, then continues (test_nudge_lets_a_stalled_agent_finish)
  - N no-progress nudges -> graceful respawn         (test_idle_generation_ends_and_respawns)
  - orphaned children cleaned up across a boundary   (test_orphaned_child_reaped_at_end)
  - substrate-observed progress stamps + reaping      (unit tests below)

Watchdog-driven cases run the real runner subprocess (so the monitor watches that
subprocess's own subtree); the nudge/idle/orphan cases drive the runner in-process
with injected providers. No SDK, no network, no spend.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from runner import Exit, Runner
from substrate import body as body_mod
from substrate import liveness
from substrate.config import Config
from substrate.metering import make_metered_complete
from substrate.state_store import StateStore
from watchdog import Watchdog


def _spawner(cfg: Config, procs: list, sequence: list[str], **extra_env: str):
    """A watchdog spawn() that drives KAIZEN_M0_BODY from `sequence` (then 'stub')."""
    it = iter(sequence)

    def spawn() -> subprocess.Popen:
        env = dict(os.environ)
        env["KAIZEN_M0_BODY"] = next(it, "stub")
        env.update(extra_env)
        p = subprocess.Popen(
            [sys.executable, str(cfg.runner_script), "--root", str(cfg.root)],
            env=env,
        )
        procs.append(p)
        return p

    return spawn


# --- 1. progress past the old 30s window is NOT reaped ----------------------


def test_progress_keeps_generation_alive(lineage_repo: Path) -> None:
    # Monitor effectively off (poll=100s) so ONLY per-tool-call progress stamps
    # matter. The body works ~4s of spaced tool calls under a 3s backstop: without
    # progress stamping it would be reaped; with it, it blesses on the first spawn.
    cfg = Config(
        root=lineage_repo,
        max_generations=1,
        heartbeat_timeout_seconds=3.0,
        liveness_poll_seconds=100.0,
        min_respawn_seconds=0,
        supervise_poll_seconds=0.1,
    )
    procs: list[subprocess.Popen] = []
    spawn = _spawner(
        cfg,
        procs,
        ["progress"],
        KAIZEN_PROGRESS_STEPS="6",
        KAIZEN_PROGRESS_SLEEP="0.4",
    )
    reason = Watchdog(cfg, spawn=spawn, max_total_spawns=4).run()

    assert reason == "generation cap reached"
    assert StateStore(cfg).load_status().generation == 1
    # Blessed on the FIRST spawn: the working generation was never reaped.
    assert len(procs) == 1


# --- 2. a long-running child keeps the parent alive (tree observation) -------


def test_busy_child_keeps_parent_alive(lineage_repo: Path) -> None:
    # One ~2.5s CPU-busy child, no tool/LLM stamps in the window, 1.5s backstop.
    # Only the monitor seeing the active child can keep the parent alive.
    cfg = Config(
        root=lineage_repo,
        max_generations=1,
        heartbeat_timeout_seconds=1.5,
        liveness_poll_seconds=0.3,
        min_respawn_seconds=0,
        supervise_poll_seconds=0.1,
    )
    procs: list[subprocess.Popen] = []
    spawn = _spawner(cfg, procs, ["busychild"], KAIZEN_BUSY_SECONDS="2.5")
    reason = Watchdog(cfg, spawn=spawn, max_total_spawns=4).run()

    assert reason == "generation cap reached"
    assert StateStore(cfg).load_status().generation == 1
    # Blessed on the FIRST spawn: the active child kept it off the backstop.
    assert len(procs) == 1


# --- 3. a true wedge (no progress, no active child) IS reaped ----------------


def test_wedged_generation_reaped(lineage_repo: Path) -> None:
    # The hang body sleeps with no tool calls, no LLM returns, and no children, so
    # nothing stamps progress -> the backstop reaps it; a fresh stub then blesses.
    cfg = Config(
        root=lineage_repo,
        max_generations=1,
        heartbeat_timeout_seconds=0.8,
        liveness_poll_seconds=0.3,
        min_respawn_seconds=0,
        supervise_poll_seconds=0.05,
    )
    procs: list[subprocess.Popen] = []
    spawn = _spawner(cfg, procs, ["hang", "stub"], KAIZEN_M0_HANG_SECONDS="30")
    reason = Watchdog(cfg, spawn=spawn, max_total_spawns=4).run()

    assert reason == "generation cap reached"
    assert StateStore(cfg).load_status().generation == 1
    # The wedged runner was reaped (killed), then a fresh one blessed: >= 2 spawns.
    assert len(procs) >= 2
    assert procs[0].returncode is not None  # the wedge really died


# --- 4. stop-without-terminate is nudged, then can finish --------------------


def _provider_factory(script):
    """A provider_factory whose complete() replays `script(call_index) -> Completion`."""

    def factory(config, meter):  # noqa: ANN001
        calls = {"n": 0}

        def complete(model, messages, tools=None):  # noqa: ANN001
            i = calls["n"]
            calls["n"] += 1
            return script(i)

        return complete

    return factory


def _bless_initial(cfg: Config) -> str:
    store = StateStore(cfg)
    store.ensure_dirs()
    good = subprocess.run(
        ["git", "-C", str(cfg.root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    store.write_last_good(good)
    return good


def test_nudge_lets_a_stalled_agent_finish(lineage_repo: Path) -> None:
    cfg = Config(root=lineage_repo)
    _bless_initial(cfg)
    store = StateStore(cfg)
    lines: list[str] = []

    from spine.provider import Completion, ToolCall

    def script(i: int):
        if i == 0:
            # turn 1: stop WITHOUT terminating and WITHOUT any tool call (idle turn).
            return Completion(content="I think I'm done?", tool_calls=[])
        # turn 2 (after the nudge): terminate.
        return Completion(
            content="ok, terminating",
            tool_calls=[
                ToolCall(
                    id="t",
                    name="terminate",
                    arguments={"reason": "finished after nudge", "wake_after": 0},
                )
            ],
        )

    code = Runner(
        cfg,
        body_runner=body_mod.run_real_body,
        provider_factory=_provider_factory(script),
        log=lines.append,
    ).run_one_generation()

    assert code == int(Exit.GRACEFUL)
    assert store.load_status().generation == 1  # nudged, then blessed
    assert any("nudg" in ln.lower() for ln in lines), lines


# --- 5. N no-progress nudges -> graceful respawn -----------------------------


def test_idle_generation_ends_and_respawns(lineage_repo: Path) -> None:
    cfg = Config(root=lineage_repo, idle_nudge_limit=3)
    good = _bless_initial(cfg)
    store = StateStore(cfg)
    lines: list[str] = []

    from spine.provider import Completion

    # Never terminates, never calls a tool: every turn is a no-progress nudge.
    code = Runner(
        cfg,
        body_runner=body_mod.run_real_body,
        provider_factory=_provider_factory(
            lambda i: Completion(content="still thinking...", tool_calls=[])
        ),
        log=lines.append,
    ).run_one_generation()

    # Ended gracefully without blessing; the watchdog will respawn a fresh one.
    assert code == int(Exit.ROLLED_BACK)
    assert store.load_status().generation == 0
    assert store.read_last_good() == good  # nothing blessed
    assert any("idle: ended after 3 no-progress nudges" in ln for ln in lines), lines


# --- 6. orphaned children cleaned up across a generation boundary ------------


def test_orphaned_child_reaped_at_end(lineage_repo: Path) -> None:
    cfg = Config(root=lineage_repo)
    _bless_initial(cfg)
    holder: list[int] = []

    def body_that_leaks_a_child(ctx) -> None:  # noqa: ANN001
        # Start a long-lived child and "forget" it, then terminate. The runner must
        # reap it at generation end so it doesn't leak into the next life.
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        holder.append(child.pid)
        ctx.sink.requested = True
        ctx.sink.reason = "leaked a child"
        ctx.sink.wake_after = 0

    code = Runner(cfg, body_runner=body_that_leaks_a_child).run_one_generation()

    assert code == int(Exit.GRACEFUL)
    assert holder, "body did not spawn a child"
    import psutil

    assert not psutil.pid_exists(holder[0]), "leaked child was not reaped"


# --- unit: substrate-observed progress + reaping ----------------------------


def test_child_subtree_active_detects_busy_child() -> None:
    busy = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time\nt=time.time()+3\nwhile time.time()<t: pass",
        ]
    )
    try:
        last: dict[int, float] = {}
        liveness.child_subtree_active(os.getpid(), last)  # prime (newly seen)
        time.sleep(0.4)
        assert liveness.child_subtree_active(os.getpid(), last) is True  # CPU advanced
    finally:
        busy.kill()
        busy.wait()


def test_reap_new_children_kills_only_new() -> None:
    before = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    baseline = liveness.snapshot_children(os.getpid())  # includes `before`
    after = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        killed = liveness.reap_new_children(os.getpid(), baseline)
        assert killed >= 1
        after.wait(timeout=5)
        assert after.returncode is not None  # the new child was reaped
        assert before.poll() is None  # the pre-existing child was spared
    finally:
        for p in (before, after):
            try:
                p.kill()
                p.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass


def test_metering_stamps_progress_on_llm_return() -> None:
    from types import SimpleNamespace

    ticks = {"n": 0}
    meter = __import__("substrate.metering", fromlist=["CostMeter"]).CostMeter()

    def raw(**kwargs):  # noqa: ANN003
        msg = SimpleNamespace(content="hi", tool_calls=[])
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg)],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=2),
            model=kwargs["model"],
        )

    complete = make_metered_complete(
        meter,
        raw_complete=raw,
        cost_fn=lambda _r: 0.001,
        on_progress=lambda: ticks.__setitem__("n", ticks["n"] + 1),
    )
    complete("openai/gpt-4o-mini", [])
    assert ticks["n"] == 1  # the LLM return stamped progress exactly once
