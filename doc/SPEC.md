# Kaizen — a self-evolving agent on the spine backbone

> Project name `Kaizen` is taken from your message; it's used lightly throughout
> so a rename is a three-instance find-and-replace. Swap it freely if the voice
> transcription garbled it.

## What this is

`Kaizen` is a self-evolving agent built on top of [spine](https://github.com/dannychantszfong/Spine),
the minimal four-tool agent backbone. The agent runs, works on its own codebase,
and when it decides it has made enough progress it **terminates its own process**
and schedules its own rebirth. A new generation comes up, inherits the previous
one's memory and plans, and continues. The loop repeats until the agent runs out
of budget, breaks itself badly enough to be unrecoverable, or completes its
roadmap.

The point is not a finished product. The point is to **make the flywheel spin**
and watch where an agent goes when it is allowed to rewrite itself across
generations. We optimize for *existence and observability first*, capability and
correctness later.

The central trick: an agent can't safely rewrite itself while running, so it
doesn't. It writes the next version, dies, and is reborn by something that
outlived it. Continuity is carried by **memory and plans on disk**, not by the
running process. The "self" persists through what it remembers, not through what
executes.

## The two-layer principle

Everything here follows from one idea: **there is a mortal layer and an immortal
layer, and the mortal layer must never be able to damage the immortal one.**

- **Mortal — the agent.** The spine clone: its code, tools, prompt. This is what
  gets rewritten every generation. It is *expected* to die, often messily.
- **Immortal — the substrate.** The watchdog, the runner, the state store, the
  guardrail policy, the budget caps. The agent cannot read, write, or kill any of
  it. This is the agent's "physics": dumb, trusted, and constant.

The immortal layer is small and boring on purpose. The smart, untrusted thing
must never have power over the dumb, trusted thing.

## Architecture

Four layers, outermost (most immortal) first:

```
┌─ watchdog.py ──────────────────────────────────────────────┐  IMMORTAL
│  separate OS process, outside the agent's reach.            │  (agent cannot
│  job: is the runner alive & healthy? if not, respawn it.    │   touch any of
│  owns: the agent's PID, heartbeat check, crash-loop guard,  │   this layer)
│  rollback-to-last-good, wake-timer, budget/generation caps. │
│        │ launches & supervises                              │
│        ▼                                                    │
│  ┌─ runner.py ────────────────────────────────────────────┐│
│  │ one generation's lifecycle. NOT the agent — its womb.   ││
│  │ load state → run the agent → on terminate-request:      ││
│  │ boot-check next body → checkpoint → commit → exit.      ││
│  │        │ runs                                           ││
│  │        ▼                                                ││
│  │  ┌─ spine Agent (agent/) ────────────────────────────┐ ││  MORTAL
│  │  │ the loop + four tools + `terminate`.               │ ││  (the evolving
│  │  │ reads ROADMAP/TODO/MEMORY, edits its own repo,     │ ││   body — this is
│  │  │ runs its own smoke tests, requests termination.    │ ││   what changes)
│  │  │        │ edits                                     │ ││
│  │  │        ▼                                           │ ││
│  │  │   agent/  ← the spine clone: the editable body     │ ││
│  │  └────────────────────────────────────────────────────┘ ││
│  └─────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────┘
```

Note the inversion that makes the whole thing safe: the **runner and watchdog sit
outside `agent/`**, so the agent — which only ever writes inside `agent/` — cannot
rewrite its own lifecycle or its own supervisor. It can break its ability to
*request* a clean death (in which case the watchdog reaps it anyway), but it
cannot break death or rebirth themselves.

## Directory layout

```
Kaizen/                        # project root = the substrate. agent has NO write access here.
  watchdog.py                 # immortal supervisor. you write this by hand.
  runner.py                   # one generation's lifecycle. lives in the substrate, not the body.
  substrate/
    config.py                 # budget cap, max generations, min runtime, respawn delay, paths
    state/
      ROADMAP.md              # long-arc plan. sticky. human-readable.
      TODO.json               # this generation's tasks: [{id, description, check, status}]
      MEMORY.md               # compacted carry-over context
      heartbeat               # agent touches this with a timestamp; watchdog reads staleness
      wake                    # optional "respawn at <ts>" note the agent leaves for the watchdog
      last_good               # git ref of the last version that booted & ran healthy
    journal/
      gen-0000.log            # raw per-generation logs. never compacted. cheap insurance.
      gen-0001.log
  agent/                      # the spine clone. the agent's cwd and ONLY writable tree.
    src/spine/...
    main.py
    ...
```

## How it maps onto spine's seams

spine is built to be extended at four seams. Kaizen uses all four and adds nothing
to the core loop:

- **A new tool — `terminate`.** Lives in `agent/src/spine/tools/`. It does *not*
  kill the process itself; it records a termination request (and an optional
  `wake_after` seconds) and returns a `ToolResult(terminate=True)` so the current
  turn ends cleanly. The real shutdown sequence belongs to the runner, which the
  agent can't reach — so the agent can request death but can't define what death
  does.
- **The guardrail — `before_tool_call` hook.** spine ships this permissive. Kaizen
  subclasses `Hooks` and makes it the single chokepoint of policy: `bash` may not
  touch anything outside `agent/`, may not edit crontab/systemd, may not `kill`,
  and may not reach the substrate. This is where invariant #5 (agent can't edit
  its own brakes) is enforced.
- **Checkpointing — `session_end` hook.** Flush MEMORY.md and TODO.json to disk at
  the end of every run, so state survives even a turn that ends unexpectedly. Pair
  with incremental writes during the run.
- **The prompt — `prompts/system.md`.** Rewritten to explain the agent's situation:
  it is one generation in a lineage, here are its ROADMAP/TODO/MEMORY conventions,
  here is how and when to call `terminate`, here is how to verify before it does.

The loop (`agent.py`) and the original four tools stay exactly as they are.

## A generation's life

**Birth.** The watchdog launches `runner.py`. The runner loads `MEMORY.md`,
`ROADMAP.md`, and `TODO.json` from `substrate/state/`, opens `journal/gen-NNNN.log`,
writes the first heartbeat, and starts the spine agent with the carried-over state
folded into its context.

**Work.** The agent reads its roadmap and the current generation's todo, then uses
`read`/`write`/`edit`/`bash` to make progress *on its own repo*. It checkpoints
memory and todo status as it goes (not only at the end). It touches the heartbeat
file periodically so the watchdog knows it's alive and not hung.

**Graceful death.** When the agent judges it has done enough, it runs its own
smoke tests, then calls `terminate(wake_after=...)`. The turn ends. The runner now
executes the **termination protocol** (below). If the protocol's boot-check fails,
the runner refuses the death and either lets the agent keep working or rolls back.

**Dirty death.** If instead the agent crashes, hangs, runs `kill`, or exhausts the
budget mid-turn, no protocol runs — but the watchdog notices (dead PID or stale
heartbeat) and handles rebirth from the last persisted state, falling back to
`last_good` if the current body won't boot.

**Rebirth.** Either path lands in the same place: the watchdog brings up a fresh
runner, which loads the (continuously-persisted) state, and the next generation
begins. The lineage continues; only the body was replaced.

## The termination protocol (runner-owned, ordered)

The agent only *requests* this. The runner *performs* it, in this order — the
order matters:

1. **Persist state first.** Flush MEMORY.md, TODO.json, ROADMAP.md. If we die
   after this point, the next generation is not amnesiac.
2. **Boot-check the candidate body.** `python -c "import spine"` + a few-second
   smoke run in a subprocess. If it crashes or hangs → **abort the death**, log
   why, and either return control to the agent or roll back to `last_good`.
3. **Advance `last_good`.** Only now, after a clean boot-check, does the current
   commit become the new known-good rollback target.
4. **Commit & push.** Commit `agent/` with the generation's changelog; push to
   GitHub. (Token scoped to this one repo.)
5. **Leave the wake note.** Write `wake` with the respawn timestamp if `wake_after`
   was set; otherwise the watchdog respawns immediately after exit.
6. **Exit.** The process ends. The watchdog takes it from here.

If the roadmap is complete, step 5 writes a "halt" sentinel instead of a wake
time, and the watchdog does *not* respawn — that is how the loop is allowed to
*win* rather than only break.

## Invariants (the non-negotiables)

These are properties the build must never violate. They are the reason the thing
can exist at all.

1. **Run in a sandbox.** A container or throwaway VM. The agent runs and executes
   arbitrary code unsupervised; isolation protects the host and gives clean resets.
2. **The watchdog is a separate process outside `agent/`.** A monitor cannot share
   its target's fate, and the agent must not be able to reach it.
3. **Nothing becomes final without a boot-check, and there is always a `last_good`.**
   The dying agent's own tests are advisory; the substrate's boot-check is the gate.
4. **State persists continuously, not only on graceful exit.** Dirty deaths are
   the expected case; they must not erase the lineage.
5. **The agent cannot edit its guardrails or supervisor.** Policy (the hook), the
   runner, and the watchdog live outside the agent's writable tree.
6. **All scheduling and killing authority lives in the substrate.** The agent
   leaves a `wake` note; it never edits crontab and never holds its own PID.

## Build order

Build the substrate **before** any real self-evolution. Get the flywheel spinning
with a dummy body, so you debug the machine and the agent's mistakes separately,
not together.

- **M0 — Substrate, with a no-op agent.** watchdog + runner + state files + config
  + the guardrail hook + the `terminate` tool. The "agent" is a stub that writes a
  log line and terminates. **Acceptance:** the watchdog respawns the runner across
  generations; `terminate` triggers the full protocol; a deliberately-broken body
  is caught by the boot-check and rolled back to `last_good`; a `kill -9` of the
  runner is noticed and respawned; budget and generation caps halt the loop.
- **M1 — Real agent + self-edit.** Swap the stub for the spine agent with the
  evolving system prompt. It reads ROADMAP/TODO/MEMORY, edits `agent/`, runs its
  own smoke tests, checkpoints memory at `session_end`, and terminates. Per-
  generation git commit; `last_good` advances only after a healthy boot.
- **M2 — Observability & budget.** Per-generation journals, cost + generation
  counters surfaced somewhere you can watch live, a simple log viewer. Enforce the
  $-cap, the generation cap, and the minimum respawn interval in the substrate.
- **M3 — Capabilities (optional, later).** New tools the agent (or you) might add:
  a browser tool, sharper per-todo verification, richer memory compaction. This is
  where "let's see what it does" really begins.

## Risks accepted for v1

These are known and deliberately *not* solved yet — listed so the choice is
conscious, not accidental:

- **No selection pressure.** "Improve yourself" with a rewritable roadmap is a
  random walk; success is undefined. v1 is an observation, not a benchmark. Keep
  the roadmap sticky (append-and-propose, not free-rewrite) so the run stays
  legible afterward.
- **Self-graded completion.** Fenced only by minimum-runtime and max-generation
  caps in v1; real per-todo verification comes later (each todo item carries a
  machine-runnable `check`; "done" means the check passes, not the agent's say-so).
- **Compaction loss.** Mitigated only by keeping raw journals, so rot is
  recoverable.
