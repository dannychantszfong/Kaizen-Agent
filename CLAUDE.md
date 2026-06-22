# CLAUDE.md — building Kaizen on spine

This repo is a **spine clone** being grown into **Kaizen**: a self-evolving agent
that works on its own code, terminates its own process, and is reborn by an
external supervisor across generations, inheriting memory and plans each time. The
full design is in [`SPEC.md`](SPEC.md). Read it before writing anything.

You are the **coding agent that builds Kaizen**. You are *not* benki itself. Your
job is to construct the substrate and wire the agent into it, following the spec
and — above everything — never violating the invariants below.

## Read first, in this order

1. **`SPEC.md`** — the architecture, the layers, the termination protocol, the
   invariants, the build order. This is the source of truth.
2. **The spine skeleton** — `src/spine/agent.py` (the loop), `src/spine/tools/`
   (the four tools + the `Tool` protocol in `base.py`), `src/spine/hooks.py` (the
   permissive policy seam you will tighten), `src/spine/prompts/system.md`. It's
   small; hold all of it in your head before extending it.
3. **`doc/spine-spec.md` and `doc/ARCHITECTURE.md`** — the foundation's rationale
   and the copy-pasteable recipes for adding a tool / hook / skill.

Then confirm your understanding with the developer before building. Do not start
coding until the shape is agreed.

## The invariants — never violate these

These are not style preferences. They are the properties that let Kaizen exist
without bricking itself or its host. If a request seems to require breaking one,
stop and say so.

1. **Sandbox.** Kaizen is meant to run inside a container/VM. Don't write code that
   assumes — or requires — running on the host directly. Document the isolation
   setup; don't quietly depend on host state.
2. **The watchdog and runner live OUTSIDE `agent/`.** They are the immortal layer.
   The agent's writable tree is `agent/` and nothing else. Never place supervisor
   or lifecycle logic where the agent can edit it.
3. **No death is final without a boot-check, and `last_good` always exists.** The
   runner verifies the next body imports and survives a smoke run before any
   termination commits. The agent's own tests are advisory only.
4. **State persists continuously.** Memory/todo/roadmap are flushed incrementally
   and at `session_end`, never only inside the graceful-termination path. Assume
   every generation can die dirty at any moment.
5. **The agent cannot reach its own brakes.** The `before_tool_call` hook, the
   runner, and the watchdog are off-limits to the agent. The guardrail hook must
   block `bash` from: writing outside `agent/`, editing crontab/systemd, sending
   signals (`kill`), and touching `substrate/`.
6. **Scheduling and PID authority live in the substrate.** The agent leaves a
   `wake` note for the watchdog; it never edits a scheduler and never learns its
   own PID.

When in doubt, push the risky capability *outward* into the dumb substrate, never
*inward* into the smart agent.

## Build order — one milestone at a time, stop for review between

Follow `SPEC.md`'s build order. The sequencing is load-bearing: **build and prove
the substrate with a no-op agent before any real LLM edits code.** Debug the
machine and the agent's mistakes separately, never together.

- **M0** — substrate + stub agent. Watchdog, runner, state files, config, guardrail
  hook, `terminate` tool. Acceptance tests in `SPEC.md` must pass: respawn across
  generations, clean termination protocol, boot-check catches a broken body and
  rolls back to `last_good`, a `kill -9` is noticed and respawned, caps halt the
  loop. **Stop here for review.**
- **M1** — wire in the real spine agent + the evolving system prompt + memory
  checkpointing + per-generation git commits.
- **M2** — observability + budget/generation/interval enforcement.
- **M3** — capabilities (browser tool, per-todo verification) — only on request.

Do not skip ahead. A broken M0 makes everything above it un-debuggable.

## Keep spine's grain

spine is deliberately minimal and the minimalism is load-bearing. Build *with* it:

- **The four tools stay four.** `read`/`write`/`edit`/`bash`; `bash` is the escape
  hatch. Kaizen adds exactly one agent-facing tool — `terminate` — because it's a
  genuine new primitive (process lifecycle), not something `bash` should do given
  invariant #5. Resist adding more core tools; prefer skills or substrate logic.
- **Own the loop.** Don't touch `agent.py`'s loop or add a framework to the core.
  Everything Kaizen needs attaches at the seams (tool, hook, prompt) or lives in the
  substrate above the loop.
- **Rent the provider.** LLM calls stay behind `provider.complete()`.
- **Validate, don't crash** in tools; surface failures as `ToolResult`s.
- The substrate (`watchdog.py`, `runner.py`, `substrate/`) is *new* code outside
  the spine core — there you write plain, boring, obvious Python. Dumb and
  readable beats clever; this is the layer whose job is to be trustworthy.

## Style & commands

- Python 3.12+. Type hints everywhere. Pydantic for schemas. Small, obvious
  functions; clarity over cleverness.
- Install: `pip install -e ".[dev]"`  (or `uv sync --extra dev`)
- Test: `pytest` — no API key needed; the loop test drives a stubbed provider.
- Lint/format: `ruff check` / `ruff format`
- The agent's *default* model is `deepseek/deepseek-chat` (very cheap ~$0.28/$0.42
  per Mtok, capable, runs on `DEEPSEEK_API_KEY` alone, priced from litellm's cost
  map). The body can override it per generation by writing `agent/MODEL`; metering is
  provider-agnostic and prices whatever ran. The provider layer normalizes the id.

If at any point a step seems to require breaking an invariant, or the developer
asks for something that does, **stop and flag it** rather than quietly working
around it. The invariants are the project.
