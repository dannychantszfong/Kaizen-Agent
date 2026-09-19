"""M1 wiring acceptance checks - the real agent path, proven WITHOUT real spend.

Each maps to a line in the M1 done-definition:

  - the budget meter reads REAL usage         (test_metered_real_body_charges_budget)
  - a body exception is caught + rolled back   (test_body_exception_caught_and_rolled_back)
  - memory checkpoints CONTINUOUSLY, surviving (test_continuous_checkpoint_survives_dirty_death,
    a dirty death (invariant #4)                test_after_tool_call_mirrors_to_state)
  - gen-0 absence is handled + state threads    (test_gen0_bootstrap_seeds_and_blesses,
    through birth/mirror                         test_seed_and_mirror_roundtrip)
  - metering is provider-agnostic: one cap over  (test_second_provider_counts_against_same_cap,
    every provider, and a model it cannot price   test_meter_sums_spend_across_providers,
    HALTS rather than billing $0                   test_unpriceable_model_halts_lineage,
                                                   test_resolve_model_prefers_body_choice)

All of it runs on injected/stubbed providers: no SDK, no network, no dollars.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from runner import Exit, Runner
from substrate import body as body_mod
from substrate.config import Config
from substrate.guardrail import SubstrateHooks
from substrate.metering import make_metered_complete
from substrate.state_store import StateStore


def head_sha(root: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()


def _bless_initial(cfg: Config) -> str:
    """Give the lineage a last_good to be born from (what the watchdog bootstraps)."""
    store = StateStore(cfg)
    store.ensure_dirs()
    good = head_sha(cfg.root)
    store.write_last_good(good)
    return good


# --- the budget meter reads REAL usage (one mocked call, no loop) -----------


def _fake_provider_response(*, cost: float):
    """A litellm-shaped response: a `terminate` tool call + a usage block. The
    metered complete reads cost + tokens off exactly this shape."""

    def raw_complete(**kwargs):
        func = SimpleNamespace(
            name="terminate",
            arguments=json.dumps({"reason": "metered wiring test", "wake_after": 0}),
        )
        tool_call = SimpleNamespace(id="t1", function=func)
        message = SimpleNamespace(content="done", tool_calls=[tool_call])
        usage = SimpleNamespace(prompt_tokens=1000, completion_tokens=50)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)

    return raw_complete


def test_metered_real_body_charges_budget(lineage_repo: Path) -> None:
    cfg = Config(root=lineage_repo)
    good = _bless_initial(cfg)
    store = StateStore(cfg)

    # Inject a metered complete over a canned response carrying real usage + cost.
    def provider_factory(config, meter):
        return make_metered_complete(
            meter,
            model=config.model,
            raw_complete=_fake_provider_response(cost=0.01),
            cost_fn=lambda _response: 0.01,
        )

    code = Runner(
        cfg,
        body_runner=body_mod.run_real_body,
        provider_factory=provider_factory,
    ).run_one_generation()

    assert code == int(Exit.GRACEFUL)
    status = store.load_status()
    # The meter's dollars (not a stub counter) reached the budget the watchdog reads.
    assert status.budget_spent_usd == pytest.approx(0.01)
    assert status.last_generation_cost_usd == pytest.approx(0.01)
    # A clean, metered generation was blessed past the boot-check.
    assert status.generation == 1
    blessed = store.read_last_good()
    assert blessed and blessed != good


# --- a body exception is caught and rolled back -----------------------------


def test_body_exception_caught_and_rolled_back(lineage_repo: Path) -> None:
    cfg = Config(root=lineage_repo)
    good = _bless_initial(cfg)
    store = StateStore(cfg)

    def exploding_body(ctx) -> None:
        raise RuntimeError("kaboom mid-life")

    code = Runner(cfg, body_runner=exploding_body).run_one_generation()

    # The crash became a controlled dirty death, not an ungraceful runner crash.
    assert code == int(Exit.ROLLED_BACK)
    status = store.load_status()
    assert status.generation == 0
    assert status.consecutive_dirty == 1
    # Nothing was blessed; the lineage still boots from the same last_good.
    assert store.read_last_good() == good
    assert head_sha(lineage_repo) == good


# --- memory checkpoints CONTINUOUSLY and survives a dirty death --------------


def test_continuous_checkpoint_survives_dirty_death(lineage_repo: Path) -> None:
    cfg = Config(root=lineage_repo)
    _bless_initial(cfg)
    store = StateStore(cfg)

    def write_then_crash(ctx) -> None:
        from spine.provider import Completion, ToolCall

        calls = {"n": 0}

        def complete(model, messages, tools=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return Completion(
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="w1",
                            name="write",
                            arguments={
                                "path": "MEMORY.md",
                                "content": "remember: this generation got here",
                            },
                        )
                    ],
                )
            raise RuntimeError("crash AFTER writing memory")

        agent = body_mod.build_agent(
            ctx.config, sink=ctx.sink, hooks=ctx.hooks, complete=complete
        )
        agent.run("write memory, then die")

    code = Runner(cfg, body_runner=write_then_crash).run_one_generation()

    assert code == int(Exit.ROLLED_BACK)
    # The mid-run write was mirrored to canonical state BEFORE the crash, so the
    # lineage's memory survived a death that never reached a graceful exit (#4).
    persisted = store.read_memory()
    assert "this generation got here" in persisted
    assert store.load_status().generation == 0


def test_after_tool_call_mirrors_to_state(lineage_repo: Path) -> None:
    """Unit-level proof of the continuous path: a mutating tool triggers a mirror,
    and the result is passed through untouched."""
    cfg = Config(root=lineage_repo)
    store = StateStore(cfg)
    store.ensure_dirs()
    hooks = SubstrateHooks(cfg, store)

    cfg.agent_carryover_path("MEMORY.md").write_text("checkpoint me", encoding="utf-8")
    sentinel = object()
    out = hooks.after_tool_call(SimpleNamespace(name="write"), None, sentinel, None)

    assert out is sentinel
    assert store.read_memory() == "checkpoint me"

    # A non-mutating tool does not trigger a mirror.
    cfg.state_carryover_path("MEMORY.md").unlink()
    hooks.after_tool_call(SimpleNamespace(name="read"), None, sentinel, None)
    assert not cfg.state_carryover_path("MEMORY.md").exists()


# --- gen-0 absence handled + carry-over threads through birth/mirror ---------


def test_seed_and_mirror_roundtrip(lineage_repo: Path) -> None:
    cfg = Config(root=lineage_repo)
    store = StateStore(cfg)
    store.ensure_dirs()

    # gen-0: state is empty, but a stale agent/ROADMAP.md lingers. Seeding must
    # REMOVE it so the prompt's "if ROADMAP.md does not exist" bootstrap fires.
    stale = cfg.agent_carryover_path("ROADMAP.md")
    stale.write_text("stale roadmap from a botched attempt", encoding="utf-8")
    assert store.seed_carryover_into_agent() == []
    assert not stale.exists()

    # The body writes its carry-over; mirroring lifts it to canonical state.
    cfg.agent_carryover_path("MEMORY.md").write_text("mem", encoding="utf-8")
    cfg.agent_carryover_path("ROADMAP.md").write_text("road", encoding="utf-8")
    mirrored = store.mirror_agent_to_state()
    assert set(mirrored) >= {"MEMORY.md", "ROADMAP.md"}
    assert store.read_memory() == "mem"
    assert store.read_roadmap() == "road"

    # Next birth seeds canonical state back into a fresh agent/ tree.
    cfg.agent_carryover_path("MEMORY.md").unlink()
    seeded = store.seed_carryover_into_agent()
    assert "MEMORY.md" in seeded
    assert cfg.agent_carryover_path("MEMORY.md").read_text(encoding="utf-8") == "mem"


def test_gen0_bootstrap_seeds_and_blesses(lineage_repo: Path) -> None:
    cfg = Config(root=lineage_repo)
    _bless_initial(cfg)
    store = StateStore(cfg)

    def gen0_body(ctx) -> None:
        from spine.provider import Completion, ToolCall

        queue = [
            Completion(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="r",
                        name="write",
                        arguments={
                            "path": "ROADMAP.md",
                            "content": "# Roadmap\n- milestone one\n",
                        },
                    )
                ],
            ),
            Completion(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="t",
                        name="write",
                        arguments={"path": "TODO.json", "content": "[]"},
                    )
                ],
            ),
            Completion(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="m",
                        name="write",
                        arguments={
                            "path": "MEMORY.md",
                            "content": "gen-0 wrote the first plan\n",
                        },
                    )
                ],
            ),
            Completion(
                content="bootstrapped",
                tool_calls=[
                    ToolCall(
                        id="term",
                        name="terminate",
                        arguments={"reason": "gen-0 bootstrap", "wake_after": 0},
                    )
                ],
            ),
        ]

        def complete(model, messages, tools=None):
            return queue.pop(0) if queue else Completion(content="(done)")

        agent = body_mod.build_agent(
            ctx.config, sink=ctx.sink, hooks=ctx.hooks, complete=complete
        )
        agent.run("you are generation zero")

    code = Runner(cfg, body_runner=gen0_body).run_one_generation()

    assert code == int(Exit.GRACEFUL)
    # The first plan threaded through to canonical, durable state.
    assert "milestone one" in store.read_roadmap()
    assert store.read_todo() == "[]"
    assert "first plan" in store.read_memory()
    assert store.load_status().generation == 1


# --- provider-agnostic metering: one cap over every provider ----------------


def _capturing_metered_factory(*, cost: float, seen: list[str]):
    """A provider_factory whose metered complete runs over a canned `terminate`
    response, records which model it was asked to run, and prices it at `cost`."""

    def factory(config, meter):
        def raw(**kwargs):
            seen.append(kwargs["model"])
            func = SimpleNamespace(
                name="terminate",
                arguments=json.dumps({"reason": "metered", "wake_after": 0}),
            )
            tc = SimpleNamespace(id="t1", function=func)
            msg = SimpleNamespace(content="done", tool_calls=[tc])
            usage = SimpleNamespace(prompt_tokens=800, completion_tokens=40)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=msg)],
                usage=usage,
                model=kwargs["model"],
            )

        return make_metered_complete(
            meter, model=config.model, raw_complete=raw, cost_fn=lambda _r: cost
        )

    return factory


def test_second_provider_counts_against_same_cap(lineage_repo: Path) -> None:
    cfg = Config(root=lineage_repo)
    _bless_initial(cfg)
    store = StateStore(cfg)

    # gen 0 runs on the DEFAULT model (DeepSeek) and bills $0.01.
    seen0: list[str] = []
    code0 = Runner(
        cfg,
        body_runner=body_mod.run_real_body,
        provider_factory=_capturing_metered_factory(cost=0.01, seen=seen0),
    ).run_one_generation()
    assert code0 == int(Exit.GRACEFUL)
    assert seen0 == [cfg.model], seen0  # the default model (deepseek/deepseek-chat)
    assert store.load_status().generation == 1

    # gen 1: the body switches ITSELF to a DIFFERENT provider via agent/MODEL.
    cfg.model_choice_path.write_text("openai/gpt-4o-mini", encoding="utf-8")
    seen1: list[str] = []
    code1 = Runner(
        cfg,
        body_runner=body_mod.run_real_body,
        provider_factory=_capturing_metered_factory(cost=0.02, seen=seen1),
    ).run_one_generation()
    assert code1 == int(Exit.GRACEFUL)
    assert seen1 == ["openai/gpt-4o-mini"], seen1

    # Spend from two different providers summed into the ONE total against ONE cap.
    status = store.load_status()
    assert status.generation == 2
    assert status.budget_spent_usd == pytest.approx(0.03)
    assert status.last_generation_cost_usd == pytest.approx(0.02)


def test_meter_sums_spend_across_providers() -> None:
    from substrate.metering import CostMeter

    meter = CostMeter()
    prices = {"deepseek/deepseek-chat": 0.02, "openai/gpt-4o-mini": 0.03}

    def raw(**kwargs):
        msg = SimpleNamespace(content="ok", tool_calls=[])
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg)],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
            model=kwargs["model"],
        )

    complete = make_metered_complete(
        meter, raw_complete=raw, cost_fn=lambda r: prices[r.model]
    )
    complete("deepseek/deepseek-chat", [])
    complete("openai/gpt-4o-mini", [])

    assert meter.calls == 2
    assert meter.total_usd == pytest.approx(0.05)
    assert set(meter.by_model) == set(prices)
    assert not meter.had_unpriceable


def test_unpriceable_model_halts_lineage(lineage_repo: Path) -> None:
    cfg = Config(root=lineage_repo)
    good = _bless_initial(cfg)
    store = StateStore(cfg)

    def factory(config, meter):
        def no_price(_response):  # litellm has no cost-map entry for this model
            raise RuntimeError("model not in litellm's cost map")

        return make_metered_complete(
            meter,
            model=config.model,
            raw_complete=_fake_provider_response(cost=0.0),
            cost_fn=no_price,
        )

    code = Runner(
        cfg, body_runner=body_mod.run_real_body, provider_factory=factory
    ).run_one_generation()

    # A meter blind spot is a broken circuit breaker: HALT, never bill $0.
    assert code == int(Exit.ROLLED_BACK)
    status = store.load_status()
    assert status.halted is True
    assert status.halt_reason == "unpriceable_model"
    assert status.budget_spent_usd == pytest.approx(0.0)
    wake = store.read_wake()
    assert wake is not None and wake.halt
    # Nothing blessed; the lineage stays on the same last_good.
    assert store.read_last_good() == good
    assert status.generation == 0


def test_resolve_model_prefers_body_choice(lineage_repo: Path) -> None:
    cfg = Config(root=lineage_repo)
    # gen-0: no MODEL file -> the substrate default applies.
    assert body_mod.resolve_model(cfg) == cfg.model
    # The body picks its own model (and thus provider) for next life.
    cfg.model_choice_path.write_text("openai/gpt-4o-mini\n", encoding="utf-8")
    assert body_mod.resolve_model(cfg) == "openai/gpt-4o-mini"
    # A blank choice falls back to the default, never an empty model id.
    cfg.model_choice_path.write_text("   \n", encoding="utf-8")
    assert body_mod.resolve_model(cfg) == cfg.model


def test_intra_generation_budget_halts_cleanly(lineage_repo: Path) -> None:
    # The turn loop's over_budget bound fires WITHIN one generation. It must HALT the
    # lineage cleanly (sentinel + flag), not end into a respawn the watchdog re-catches.
    cfg = Config(root=lineage_repo, budget_cap_usd=0.05)
    good = _bless_initial(cfg)
    store = StateStore(cfg)

    def factory(config, meter):
        # A turn that does NOT terminate but bills $0.10 (> the $0.05 cap).
        def raw(**kwargs):
            msg = SimpleNamespace(content="still working, not done", tool_calls=[])
            return SimpleNamespace(
                choices=[SimpleNamespace(message=msg)],
                usage=SimpleNamespace(prompt_tokens=2000, completion_tokens=100),
                model=kwargs["model"],
            )

        return make_metered_complete(
            meter, model=config.model, raw_complete=raw, cost_fn=lambda _r: 0.10
        )

    code = Runner(
        cfg, body_runner=body_mod.run_real_body, provider_factory=factory
    ).run_one_generation()

    # Halted cleanly via the sentinel, not blessed, not respawned.
    assert code == int(Exit.ROLLED_BACK)
    status = store.load_status()
    assert status.halted is True
    assert status.halt_reason == "budget"
    assert status.budget_spent_usd >= cfg.budget_cap_usd
    wake = store.read_wake()
    assert wake is not None and wake.halt
    assert store.read_last_good() == good
    assert status.generation == 0


# --- context-window fit + transient-error retry -----------------------------


def test_fit_context_keeps_small_conversation() -> None:
    from substrate.metering import fit_context

    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "hi"},
    ]
    # Under budget -> returned unchanged (same object).
    assert fit_context("deepseek/deepseek-chat", msgs) is msgs


def test_fit_context_trims_long_conversation_pairing_safe() -> None:
    from substrate.metering import _est_msg_tokens, fit_context

    system = {"role": "system", "content": "SYSTEM"}
    msgs = [system]
    for i in range(60):  # 60 big assistant(tool_call)+tool pairs => far over any window
        msgs.append(
            {
                "role": "assistant",
                "content": "x" * 4000,
                "tool_calls": [
                    {
                        "id": f"t{i}",
                        "type": "function",
                        "function": {"name": "bash", "arguments": "{}"},
                    }
                ],
            }
        )
        msgs.append({"role": "tool", "tool_call_id": f"t{i}", "content": "y" * 4000})

    trimmed = fit_context("unknown/model-xyz", msgs)  # unknown -> default 30k limit
    assert trimmed[0] is system  # system message always kept
    assert len(trimmed) < len(msgs)  # actually trimmed
    assert trimmed[1]["role"] != "tool"  # never a leading orphaned tool result
    assert sum(_est_msg_tokens(m) for m in trimmed) <= int(30000 * 0.7)


def test_metered_complete_retries_transient_then_succeeds() -> None:
    from substrate.metering import CostMeter, make_metered_complete

    class RateLimitError(Exception):
        pass

    calls = {"n": 0}

    def raw(**kwargs):
        calls["n"] += 1
        if calls["n"] < 2:
            raise RateLimitError("slow down")
        msg = SimpleNamespace(content="ok", tool_calls=[])
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg)],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=2),
            model=kwargs["model"],
        )

    meter = CostMeter()
    complete = make_metered_complete(meter, raw_complete=raw, cost_fn=lambda _r: 0.001)
    complete("deepseek/deepseek-chat", [{"role": "user", "content": "hi"}])
    assert calls["n"] == 2  # retried the transient error once, then succeeded
    assert meter.calls == 1


def test_metered_complete_propagates_non_transient() -> None:
    from substrate.metering import CostMeter, make_metered_complete

    class BadRequestError(Exception):
        pass

    calls = {"n": 0}

    def raw(**kwargs):
        calls["n"] += 1
        raise BadRequestError("malformed")

    meter = CostMeter()
    complete = make_metered_complete(meter, raw_complete=raw, cost_fn=lambda _r: 0.0)
    with pytest.raises(BadRequestError):
        complete("deepseek/deepseek-chat", [{"role": "user", "content": "hi"}])
    assert calls["n"] == 1  # non-transient errors are not retried
