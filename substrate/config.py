"""substrate.config — the dumb, constant knobs and paths for one lineage.

Everything in the substrate derives its locations from a single `Config.root`, so
the whole machine can be pointed at a throwaway repo (the M0 acceptance tests do
exactly this) without touching the real one. Overridable caps/timings persist to
`state/config.json` so the watchdog and the runner subprocesses it spawns share
one source of truth. Nothing here is clever; this is the layer whose job is to be
trustworthy.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

# Fields the watchdog may set and a spawned runner must inherit. Paths are derived
# from `root`, never serialized.
_OVERRIDABLE = (
    "model",
    "budget_cap_usd",
    "max_generations",
    "crash_loop_threshold",
    "min_respawn_seconds",
    "heartbeat_timeout_seconds",
    "boot_check_timeout_seconds",
    "supervise_poll_seconds",
    "liveness_poll_seconds",
    "idle_nudge_limit",
    "log_transcript",
    "transcript_max_chars",
    "stub_cost_per_generation_usd",
)

# The three files that cross a generation's death. The body writes them in its
# cwd (agent/); the substrate mirrors them to the canonical, durable copy under
# state/ and seeds them back into agent/ at each birth. Names are identical on
# both sides, so a name maps cleanly to one agent path and one state path.
CARRYOVER_FILENAMES = ("MEMORY.md", "ROADMAP.md", "TODO.json")


@dataclass(frozen=True)
class Config:
    root: Path

    # -- the DEFAULT model, used until the body chooses its own (see
    # `model_choice_path`). DeepSeek's chat model: very cheap (~$0.28/$0.42 per Mtok)
    # to stretch the budget, capable enough to self-evolve, and gen-0 runs with ONLY
    # DEEPSEEK_API_KEY set. litellm prices it from its cost map (cost_per_token), so
    # the metering circuit breaker does not false-trip; api.deepseek.com is on the
    # egress allowlist. The body can switch to any other allowlisted provider.
    model: str = "deepseek/deepseek-chat"

    # -- caps: the brakes the watchdog enforces (authority lives in the substrate).
    # budget_cap_usd and max_generations are LOOSE-BUT-FINITE runaway fuses: high
    # enough not to bind normal operation, never infinite. The real spend limit is
    # the API key's funding + the provider-side cap, not this number; this just stops
    # an unattended runaway. Tune freely.
    budget_cap_usd: float = 100.0
    max_generations: int = 1000
    crash_loop_threshold: int = 5

    # -- timing
    # min_respawn_seconds is the pacing proxy: the minimum gap between consecutive
    # spawns, so a fast-terminating lineage can't spin. (A true minimum-runtime-per-
    # generation gate is a future knob; this paces respawns, not work.)
    min_respawn_seconds: float = 30.0
    # heartbeat_timeout_seconds is the GENEROUS HARD BACKSTOP — a dead-man's-switch
    # for a wedged process, NOT a work-time limit. The substrate stamps last-progress
    # on every observed unit of work (LLM returns, tool calls, an active child in the
    # process subtree), so with that reset this fires only on a true wedge: zero
    # progress AND no active child for the whole window. 30 minutes.
    heartbeat_timeout_seconds: float = 1800.0
    boot_check_timeout_seconds: float = 30.0
    supervise_poll_seconds: float = 0.1
    # how often the runner's liveness monitor samples the agent's process subtree
    # for an actively-running child (so a long build/test keeps the parent alive).
    liveness_poll_seconds: float = 5.0
    # intra-generation idle escalation: after this many consecutive no-progress
    # nudges (the agent stops without terminating and does nothing when asked to
    # continue), end the generation gracefully and respawn a fresh one.
    idle_nudge_limit: int = 3
    # observability: log the agent's transcript — the prompts it receives, its
    # assistant text, each tool call (name + full args, including file contents it
    # writes), and each result — through the journal/stdout, with API-key redaction.
    # Off makes the logs just metering + lifecycle events.
    log_transcript: bool = True
    # max chars per transcript field (assistant text / one tool arg / one result).
    # 0 = unlimited (log everything in full). Set a number to cap very large fields.
    transcript_max_chars: int = 0

    # -- M0 only: a stubbed per-generation cost so the budget cap is exercisable
    # without a real LLM. M2 replaces this with measured spend.
    stub_cost_per_generation_usd: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).resolve())

    # -- derived paths (the directory layout from SPEC) ---------------------
    @property
    def agent_dir(self) -> Path:
        return self.root / "agent"

    @property
    def agent_src(self) -> Path:
        return self.agent_dir / "src"

    @property
    def substrate_dir(self) -> Path:
        return self.root / "substrate"

    @property
    def state_dir(self) -> Path:
        return self.substrate_dir / "state"

    @property
    def journal_dir(self) -> Path:
        return self.substrate_dir / "journal"

    @property
    def roadmap_path(self) -> Path:
        return self.state_dir / "ROADMAP.md"

    @property
    def todo_path(self) -> Path:
        return self.state_dir / "TODO.json"

    @property
    def memory_path(self) -> Path:
        return self.state_dir / "MEMORY.md"

    @property
    def model_choice_path(self) -> Path:
        """The body's own model selection. Lives in agent/ (committed body state),
        so the agent can switch models — and providers — across generations; absent
        on gen-0, where `model` (the default) applies. Metering is provider-agnostic,
        so whatever the body picks here is priced off the real response."""
        return self.agent_dir / "MODEL"

    # -- carry-over: the body's working copy (agent/) vs the canonical copy (state/)
    def agent_carryover_path(self, name: str) -> Path:
        """Where the body reads/writes a carry-over file (its cwd is agent/)."""
        return self.agent_dir / name

    def state_carryover_path(self, name: str) -> Path:
        """The durable, canonical copy the substrate mirrors to and seeds from."""
        return self.state_dir / name

    @property
    def heartbeat_path(self) -> Path:
        return self.state_dir / "heartbeat"

    @property
    def wake_path(self) -> Path:
        return self.state_dir / "wake"

    @property
    def last_good_path(self) -> Path:
        return self.state_dir / "last_good"

    @property
    def status_path(self) -> Path:
        return self.state_dir / "status.json"

    @property
    def config_path(self) -> Path:
        return self.state_dir / "config.json"

    def journal_path(self, generation: int) -> Path:
        return self.journal_dir / f"gen-{generation:04d}.log"

    @property
    def bootcheck_script(self) -> Path:
        return self.substrate_dir / "bootcheck.py"

    @property
    def runner_script(self) -> Path:
        return self.root / "runner.py"

    # -- persistence of the overridable knobs -------------------------------
    def to_overrides(self) -> dict[str, float | int]:
        return {k: getattr(self, k) for k in _OVERRIDABLE}

    def save(self) -> None:
        """Persist overridable knobs so spawned runners inherit them."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.to_overrides(), indent=2), encoding="utf-8")
        os.replace(tmp, self.config_path)

    @classmethod
    def load(cls, root: str | Path) -> "Config":
        """Build a Config for `root`, folding in any persisted overrides."""
        root = Path(root)
        cfgfile = root / "substrate" / "state" / "config.json"
        overrides: dict = {}
        if cfgfile.exists():
            try:
                raw = json.loads(cfgfile.read_text(encoding="utf-8"))
                overrides = {k: v for k, v in raw.items() if k in _OVERRIDABLE}
            except (json.JSONDecodeError, OSError):
                overrides = {}
        return cls(root=root, **overrides)
