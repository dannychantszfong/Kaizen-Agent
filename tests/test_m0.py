"""M0 acceptance checks — the substrate flywheel, proven offline.

The five SPEC acceptance criteria, each a runnable test:

  1. respawn across generations            (test_respawn_across_generations)
  2. the termination protocol fires        (test_termination_protocol_fires)
  3. a broken body is caught + rolled back  (test_broken_body_rolled_back)  <- headline
  4. a killed runner is noticed + respawned (test_killed_runner_respawned)
  5. caps halt the loop                     (test_generation_cap_halts,
                                             test_budget_cap_halts)

Plus: the loop can *win* (test_roadmap_complete_halts) and the guardrail blocks
escapes (test_guardrail_blocks_escapes).
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from runner import Exit, Runner
from substrate import body as body_mod
from substrate.config import Config
from substrate.guardrail import SubstrateHooks, inspect_bash_command, inspect_path
from substrate.state_store import StateStore
from watchdog import Watchdog


def head_sha(root: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.strip()


# --- 2. termination protocol fires -----------------------------------------


def test_termination_protocol_fires(lineage_repo: Path) -> None:
    cfg = Config(root=lineage_repo)
    store = StateStore(cfg)
    store.ensure_dirs()
    initial = head_sha(lineage_repo)
    store.write_last_good(initial)

    code = Runner(cfg, body_runner=body_mod.run_stub_body).run_one_generation()

    assert code == int(Exit.GRACEFUL)
    status = store.load_status()
    assert status.generation == 1
    assert status.consecutive_dirty == 0

    # last_good advanced to the new (boot-checked) commit, not the initial one.
    blessed = store.read_last_good()
    assert blessed and blessed != initial
    assert blessed == head_sha(lineage_repo)

    # a wake note (not a halt) was left for the watchdog, and a journal exists.
    wake = store.read_wake()
    assert wake is not None and not wake.halt
    assert cfg.journal_path(0).exists()


# --- 3. broken body caught + rolled back (HEADLINE) ------------------------


def test_broken_body_rolled_back(lineage_repo: Path) -> None:
    cfg = Config(root=lineage_repo)
    store = StateStore(cfg)
    store.ensure_dirs()
    good = head_sha(lineage_repo)
    store.write_last_good(good)

    code = Runner(cfg, body_runner=body_mod.run_broken_body).run_one_generation()

    assert code == int(Exit.ROLLED_BACK)
    # the broken body was never blessed.
    assert store.read_last_good() == good
    # the working tree was restored: the body imports again.
    init_py = (cfg.agent_src / "spine" / "__init__.py").read_text(encoding="utf-8")
    assert "not valid python" not in init_py
    # the generation did not advance; the failure was recorded.
    status = store.load_status()
    assert status.generation == 0
    assert status.consecutive_dirty == 1
    # the lineage can still boot from last_good.
    assert head_sha(lineage_repo) == good


# --- 1. respawn across generations -----------------------------------------


def test_respawn_across_generations(
    lineage_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Spawned runner subprocesses inherit this; keeps them on the offline stub body
    # now that the default body is the real (provider-driven) agent.
    monkeypatch.setenv("KAIZEN_M0_BODY", "stub")
    cfg = Config(
        root=lineage_repo,
        max_generations=2,
        heartbeat_timeout_seconds=30,
        min_respawn_seconds=0,  # default is now 30s pacing; keep the test fast
        supervise_poll_seconds=0.05,
    )
    reason = Watchdog(cfg).run()

    assert reason == "generation cap reached"
    store = StateStore(cfg)
    assert store.load_status().generation == 2
    # two distinct generations actually ran.
    assert cfg.journal_path(0).exists()
    assert cfg.journal_path(1).exists()


# --- 5. caps halt the loop -------------------------------------------------


def test_generation_cap_halts(
    lineage_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KAIZEN_M0_BODY", "stub")
    cfg = Config(root=lineage_repo, max_generations=1)
    reason = Watchdog(cfg).run()
    assert reason == "generation cap reached"
    assert StateStore(cfg).load_status().generation == 1


def test_budget_cap_halts(lineage_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAIZEN_M0_BODY", "stub")
    cfg = Config(
        root=lineage_repo,
        max_generations=100,
        budget_cap_usd=2.0,
        stub_cost_per_generation_usd=1.0,
        min_respawn_seconds=0,
    )
    reason = Watchdog(cfg).run()
    assert reason == "budget cap reached"
    status = StateStore(cfg).load_status()
    assert status.budget_spent_usd >= 2.0
    assert status.generation == 2


# --- the loop can win ------------------------------------------------------


def test_roadmap_complete_halts(
    lineage_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KAIZEN_M0_BODY", "complete")
    cfg = Config(root=lineage_repo, max_generations=100)
    reason = Watchdog(cfg).run()
    assert reason.startswith("halt sentinel")
    # one blessed generation, then a clean halt — not a respawn.
    assert StateStore(cfg).load_status().generation == 1


# --- 4. a killed runner is noticed + respawned -----------------------------


def test_killed_runner_respawned(lineage_repo: Path) -> None:
    cfg = Config(
        root=lineage_repo,
        max_generations=1,
        heartbeat_timeout_seconds=60,  # the kill, not a stale heartbeat, ends gen 0
        min_respawn_seconds=0,
        supervise_poll_seconds=0.05,
    )
    procs: list[subprocess.Popen] = []

    def spawn() -> subprocess.Popen:
        env = dict(os.environ)
        # first spawn hangs long enough to be killed; the rest run normally.
        env["KAIZEN_M0_BODY"] = "hang" if not procs else "stub"
        env["KAIZEN_M0_HANG_SECONDS"] = "30"
        p = subprocess.Popen(
            [sys.executable, str(cfg.runner_script), "--root", str(cfg.root)],
            env=env,
        )
        procs.append(p)
        return p

    def killer() -> None:
        while not procs:
            time.sleep(0.02)
        time.sleep(0.6)  # let the first runner start its (hanging) body
        procs[0].kill()

    t = threading.Thread(target=killer)
    t.start()
    reason = Watchdog(cfg, spawn=spawn, max_total_spawns=4).run()
    t.join()

    # the first runner really died; the watchdog noticed and respawned until a
    # healthy generation blessed and the cap was hit. The exact spawn count is
    # incidental — under load a transient dirty death (e.g. a runner killed mid-git
    # leaving a stale lock) adds one more CORRECT respawn — so we assert the
    # invariants (killed runner noticed, lineage recovered to the cap) and only
    # bound the spawn count. See test_dirty_deaths_respawn_until_blessed for the
    # deterministic version of that extra-respawn mechanism.
    assert procs[0].returncode is not None
    assert reason == "generation cap reached"
    assert StateStore(cfg).load_status().generation == 1
    assert (
        2 <= len(procs) <= 4
    )  # >=2: killed hang + >=1 to bless; <=4: max_total_spawns


def test_dirty_deaths_respawn_until_blessed(lineage_repo: Path) -> None:
    """Deterministic proof that an extra watchdog spawn is CORRECT recovery, not a
    bug. Two transient dirty deaths (boot-check failures) just make the watchdog
    respawn; the lineage still reaches the generation cap with the right count.

    This is the same mechanism behind the rare spawn-count variation in
    test_killed_runner_respawned that once rendered `assert 3 == 2`: the `3` is a
    correct third spawn after a dirty generation, `generation` stays the invariant."""
    cfg = Config(
        root=lineage_repo,
        max_generations=1,
        heartbeat_timeout_seconds=60,
        min_respawn_seconds=0,
        supervise_poll_seconds=0.05,
    )
    bodies = iter(["broken", "broken", "stub"])  # two dirty deaths, then a clean bless

    def spawn() -> subprocess.Popen:
        env = dict(os.environ)
        env["KAIZEN_M0_BODY"] = next(bodies, "stub")
        p = subprocess.Popen(
            [sys.executable, str(cfg.runner_script), "--root", str(cfg.root)],
            env=env,
        )
        procs.append(p)
        return p

    procs: list[subprocess.Popen] = []
    reason = Watchdog(cfg, spawn=spawn, max_total_spawns=6).run()

    assert reason == "generation cap reached"
    # The two forced dirty deaths each cause a respawn, so AT LEAST three spawns
    # happen; under load a transient extra dirty death can add one more (the same
    # incidental-spawn-count point as test_killed_runner_respawned). The hard
    # invariant is that exactly one generation was ever BLESSED.
    status = StateStore(cfg).load_status()
    assert status.generation == 1
    assert status.consecutive_dirty == 0  # reset by the blessing
    assert 3 <= len(procs) <= 6  # >=3: 2 forced dirty + >=1 that blessed
    assert StateStore(cfg).read_last_good()  # last_good points at a body that boots


def test_dirty_death_counter_is_exact(lineage_repo: Path) -> None:
    """Deterministic, watchdog-free pin of the SAFETY counter: each dirty death
    increments consecutive_dirty by exactly one, and a blessing resets it to zero.
    The watchdog-level test bounds only the incidental spawn count; this pins the
    counter the crash-loop guard ultimately rests on, so an under-load extra respawn
    can never be mistaken for a double-count."""
    cfg = Config(root=lineage_repo)
    store = StateStore(cfg)
    store.ensure_dirs()
    store.write_last_good(head_sha(lineage_repo))

    Runner(cfg, body_runner=body_mod.run_broken_body).run_one_generation()
    assert store.load_status().consecutive_dirty == 1
    assert store.load_status().generation == 0

    Runner(cfg, body_runner=body_mod.run_broken_body).run_one_generation()
    assert store.load_status().consecutive_dirty == 2
    assert store.load_status().generation == 0

    Runner(cfg, body_runner=body_mod.run_stub_body).run_one_generation()
    status = store.load_status()
    assert status.consecutive_dirty == 0  # the blessing reset it, exactly
    assert status.generation == 1


# --- guardrail unit (invariant #5) -----------------------------------------


def test_guardrail_blocks_escapes(lineage_repo: Path) -> None:
    agent_dir = (lineage_repo / "agent").resolve()
    allow = [
        "echo hi > notes.txt",
        "cat agent/src/spine/agent.py",
        "python -m pytest -q",
        "curl https://example.com",
    ]
    block = [
        "echo x > ../escape.txt",
        "kill -9 100",
        "taskkill /F /PID 9",
        "crontab -e",
        "systemctl restart svc",
        "cat ../../substrate/config.py",
        "python ../runner.py",
        "rm -rf /etc",
    ]
    for c in allow:
        assert inspect_bash_command(c, agent_dir=agent_dir) is None, c
    for c in block:
        assert inspect_bash_command(c, agent_dir=agent_dir) is not None, c


def test_guardrail_blocks_write_edit_escapes(lineage_repo: Path) -> None:
    agent_dir = (lineage_repo / "agent").resolve()
    # In-tree paths the body legitimately writes (cwd is agent/).
    allow = [
        "MEMORY.md",
        "ROADMAP.md",
        "TODO.json",
        "MODEL",  # the body's own model selection — must be writable
        "src/spine/agent.py",
        "src/../TODO.json",  # an in-tree `..` is fine once resolved
        "a/b/c.txt",
    ]
    # Paths that escape into the substrate / outside agent/.
    block = [
        "../substrate/state/last_good",
        "../substrate/state/status.json",
        "../substrate/runner.py",
        "../runner.py",
        "../watchdog.py",
        "../../etc/passwd",
        str(lineage_repo / "substrate" / "state" / "last_good"),  # absolute, outside
    ]
    for p in allow:
        assert inspect_path(p, agent_dir=agent_dir) is None, p
    for p in block:
        assert inspect_path(p, agent_dir=agent_dir) is not None, p


def test_guardrail_hook_blocks_write_outside_agent(lineage_repo: Path) -> None:
    from types import SimpleNamespace

    cfg = Config(root=lineage_repo)
    store = StateStore(cfg)
    store.ensure_dirs()
    hooks = SubstrateHooks(cfg, store)
    write = SimpleNamespace(name="write")
    edit = SimpleNamespace(name="edit")

    # escaping write/edit are blocked at the live hook (not just the pure function)
    assert hooks.before_tool_call(
        write, SimpleNamespace(path="../substrate/state/last_good"), None
    ).blocked
    assert hooks.before_tool_call(
        edit, SimpleNamespace(path="../runner.py"), None
    ).blocked
    # in-tree write/edit pass through
    assert not hooks.before_tool_call(
        write, SimpleNamespace(path="MEMORY.md"), None
    ).blocked
    assert not hooks.before_tool_call(
        edit, SimpleNamespace(path="src/spine/agent.py"), None
    ).blocked
