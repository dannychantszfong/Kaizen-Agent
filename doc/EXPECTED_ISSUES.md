# Expected issues — what could go wrong when a weak model runs this unattended

> Honest predictions for a real run on `deepseek/deepseek-chat` (cheap, capable-ish,
> deliberately weak) left running by itself. Grounded in the actual code and the exact
> log strings it emits. Meant to be read against the real logs later, and against an
> independent forecast. Written before any live generation.

## How to read this

Each item lists: **trigger**, **log signature** (what to grep), **containment**, and
**bug vs designed**.

- **Containment** — `SELF-RECOVERS` (the substrate rolls back/respawns and continues),
  `HALTS→HUMAN` (the lineage stops cleanly and waits for you), or `RUNS-WRONG` (it
  keeps going but in a degraded/incorrect way — the dangerous category, because
  nothing stops it).
- **Bug vs designed** — `DESIGNED` (intended behavior, even if it looks alarming),
  `ACCEPTED` (a known v1 limitation, see SPEC "Risks accepted"), or `GAP` (a real
  weakness worth fixing).

The two-layer safety model means **almost nothing is truly fatal**: the watchdog,
the boot-check + `last_good`, and the OS isolation bound the blast radius. "Fatal"
here means *the lineage halts and can't restart itself* — you still have the volume.

---

## 1. Bootstrap / generation zero

**Gen-0 never writes a usable ROADMAP, or writes garbage.** A weak model misreads the
self-authoring prompt and produces a vague/empty/over-ambitious roadmap.
- Log: `gen 0` journal shows `write` calls + `terminate`; `substrate/state/ROADMAP.md`
  holds whatever it wrote.
- Containment: `RUNS-WRONG` — every later generation inherits a weak plan. The run is
  a random walk regardless (no selection pressure). `ACCEPTED` (SPEC: self-authored
  purpose + sticky-roadmap risk).

**Gen-0 terminates without writing ROADMAP at all.** The model calls `terminate`
immediately. The body boots, so it is *blessed* and `generation` advances — but state
still has no ROADMAP, so the next generation also sees "you are generation zero."
- Log: a string of near-identical `gen N` journals, `generation` climbing, no
  `ROADMAP.md` ever appearing in `state/`; each ends `generation N graceful`.
- Containment: `RUNS-WRONG` then `HALTS→HUMAN` — it burns cheap generations doing
  nothing until `max_generations` (1000) or the budget halts it. `GAP` (the gen-0
  conditional trusts the model to bootstrap; there is no "you've been gen-0 too many
  times" tripwire). Likely with a weak model — watch for it early.

**Gen-0 writes a ROADMAP but never terminates.** It writes files, then keeps going.
- Log: `turn N: ended without terminating ...`, then `idle: ended after 3 no-progress
  nudges` (if it goes quiet) or continued work.
- Containment: `SELF-RECOVERS` — the ROADMAP was mirrored to state on the `write`, so
  the *next* generation is correctly gen-1. `DESIGNED`.

**Malformed `TODO.json`.** The model writes invalid JSON or ignores the
`[{id,description,check,status}]` shape. The substrate never parses TODO.json — it is
opaque carry-over — so nothing in the substrate breaks; only the agent's own future
reads suffer.
- Containment: `RUNS-WRONG` (agent confusion only). `ACCEPTED`.

---

## 2. Self-modification

**A bad edit breaks `import spine`.** The headline case. The model writes a syntax
error or deletes something load-bearing, then terminates.
- Log: `protocol 3/5: BOOT-CHECK FAILED: import failed: SyntaxError ...` →
  `death aborted; rolled back to last_good` → `generation ended DIRTY: boot-check
  failed`. Watchdog: `no progress: runner exited ...`.
- Containment: `SELF-RECOVERS`. `DESIGNED` — this is the core safety net and it will
  fire **often** with a weak model. Not an error; the machine working.

**A working edit that is still wrong** (compiles/imports but is worse). The boot-check
only checks that `spine` imports and survives one offline turn — it does **not** judge
quality. So a body that imports but is dumber gets blessed.
- Log: `boot-check passed`, `blessed last_good = ...`, normal.
- Containment: `RUNS-WRONG` — quality regressions are carried forward. `ACCEPTED`
  (SPEC: advisory tests, boot-check is a liveness gate not a quality gate).

**The agent edits its own system prompt (`agent/src/spine/prompts/system.md`).** It is
tracked, self-evolvable code; a mangled prompt that still imports is blessed and
shapes all successors — including possibly breaking the gen-0 conditional or the
terminate guidance.
- Log: nothing obvious; only behavioral drift in later generations.
- Containment: `RUNS-WRONG` — "prompt rot." `GAP` (no prompt-sanity gate; recoverable
  by reverting the file in the volume).

**The agent "disables" the guardrail by editing `agent/src/spine/hooks.py`.** No
effect: the runner injects `SubstrateHooks` at construction, overriding the body's
permissive base. The agent may believe it succeeded.
- Log: `GUARDRAIL blocked bash ...` keeps appearing.
- Containment: `SELF-RECOVERS` / no-op. `DESIGNED` (invariant #5).

**The agent rewrites `agent/src/spine/agent.py`'s loop to skip hooks.** This *can*
bypass the in-process guardrail and the per-tool progress stamp. But: the boot-check
runs the rewritten loop, so a broken rewrite is caught and rolled back; a working
rewrite still cannot escape the container (read-only substrate code, non-root, egress
allowlist), and the metering stamp (in the injected `complete`) + the process-tree
monitor still keep liveness.
- Log: possibly fewer `checkpoint: mirrored ...` lines; boot-check may fail.
- Containment: `SELF-RECOVERS` (if it breaks) or `RUNS-WRONG`-but-contained (the
  container is the hard wall). `DESIGNED`/`ACCEPTED` (documented defense-in-depth).

**The agent writes to the substrate via `write`/`edit`.** Now **guarded** (closed):
`SubstrateHooks.before_tool_call` runs `inspect_path` on `write`/`edit` targets, the
same way `inspect_bash_command` guards `bash`. A target that resolves outside `agent/`
(a `..` chain that leaves the tree, an absolute path, or one naming
`substrate`/`runner.py`/`watchdog.py`) is blocked before the tool runs; an in-tree
path — including a benign in-tree `..` like `src/../MEMORY.md` — is allowed.
- Log: `GUARDRAIL blocked write '../substrate/state/last_good': Blocked: ... outside
  agent/`.
- Containment: `SELF-RECOVERS` (the write is refused and returned to the model as an
  error). Defense-in-depth in front of the OS wall (immortal code is read-only;
  state/journal/.git are agent-writable, which is what this guard now protects from a
  hook-respecting body). A loop-rewriting body that skips hooks still can't escape the
  container; that residual is `ACCEPTED`/documented.

**Premature `roadmap_complete=true`.** The model decides it is finished and sets the
flag in `terminate`.
- Log: `protocol 5/5: roadmap complete -> HALT sentinel written`; watchdog
  `halting: halt sentinel (...)`.
- Containment: `HALTS→HUMAN` — the loop "wins" early. `ACCEPTED` (self-graded
  completion is a known v1 risk). Looks like success; verify it actually finished.

---

## 3. Loop / liveness dynamics

**Nudge churn.** The model keeps ending turns without terminating but does *just
enough* (one tool call) each time to reset the no-progress counter, so it never idle-
escalates and loops indefinitely.
- Log: many `turn N: ended without terminating after work; nudging`.
- Containment: `SELF-RECOVERS` — bounded by the intra-generation budget halt and the
  cumulative budget cap. `DESIGNED` (the idle escalation only catches *no-progress*;
  productive-but-aimless churn is bounded by money, not nudges).

**True idle stall.** The model goes quiet (no tools, no terminate) for 3 nudges.
- Log: `idle: ended after 3 no-progress nudges` → restored → respawn fresh.
- Containment: `SELF-RECOVERS`. `DESIGNED`.

**A long legitimate child (build/test/server).** A `bash` running a real workload.
- Log: no reap; the monitor silently refreshes progress.
- Containment: `SELF-RECOVERS`. `DESIGNED` — the process-tree monitor is exactly for
  this. Note: `bash` has its own 120 s default timeout (max 600 s), so a hung shell
  command is usually bounded long before the 30-minute backstop.

**A truly wedged generation** (no progress, no active child — e.g. a deadlock, or a
child that consumes zero CPU like `sleep 99999` that somehow outlives the bash
timeout).
- Log: watchdog `heartbeat stale (...); reaping wedged runner` → kill subtree →
  respawn.
- Containment: `SELF-RECOVERS` after up to `heartbeat_timeout_seconds` (30 min).
  `DESIGNED` (the dead-man's-switch). The 30-minute wait is intentional generosity.

**Persistent heartbeat-write failure** (read-only mount, bad perms). Every stamp
fails → the watchdog reaps every generation as "wedged" → reap-thrash.
- Log: `WARNING: heartbeat stamp has failed N times in a row (...); ... will look like
  the watchdog reap-thrashing healthy generations as wedged.`
- Containment: `HALTS→HUMAN` (reap-thrash → crash-loop) but now **loud**. `GAP`-turned-
  visible (added the warning so it can't masquerade as wedge reaps).

---

## 4. Cost / runaway

**Steady budget burn toward the cap.** Normal operation costs money. DeepSeek is
cheap (~$0.0005 per typical metered call here), so `budget_cap_usd` ($100) is a huge
number of calls; `max_generations` (1000) usually binds first.
- Log: per call `[meter] deepseek · deepseek/deepseek-chat · $0.000x · lineage $Y /
  $100.00`; per gen `gen N cost $... lineage total $... of $100.00 cap`.
- Containment: `HALTS→HUMAN` at the fuse. `DESIGNED`. Real spend limit is the key's
  funding + provider cap, not this number.

**One generation tries to burn the whole budget** (never terminates, keeps spending).
- Log: `turn N: budget cap reached mid-generation; halting the lineage` → `budget cap
  reached mid-generation (... >= $100.00); halting`.
- Containment: `HALTS→HUMAN` cleanly (sentinel set). `DESIGNED` (the intra-generation
  bound; the watchdog only checks caps between generations, so the runner enforces it
  within one).

**The agent switches to a model litellm cannot price** (a typo, a brand-new id not in
litellm's cost map).
- Log: `[meter] ... · UNPRICEABLE (... tokens) — HALTING (broken circuit breaker)` →
  `unpriceable model(s) ... halting the lineage`.
- Containment: `HALTS→HUMAN`. `DESIGNED` — a blind meter is a broken cap; halting beats
  billing $0. (Verified the default `deepseek/deepseek-chat` *is* priced, so gen-0
  will not false-trip.)

**The agent switches to an unkeyed or expensive provider via `agent/MODEL`.** If it
writes e.g. `openai/gpt-4o` and that key is unset, every call fails auth; if the
choice is blessed, `last_good` carries the bad model and the lineage can't self-revert.
- Log: `body crashed: ... AuthenticationError ...` repeating → watchdog `crash-loop
  threshold reached`. Or, if keyed, a sudden cost jump in the `[meter]` lines.
- Containment: `HALTS→HUMAN` (crash-loop) but **stuck** — recovery needs you to edit
  `agent/MODEL` in the volume. `GAP` — model self-selection is powerful and a weak
  model can brick the lineage onto a bad model. Consider restricting the allowed model
  set, or only honoring `agent/MODEL` for keyed+priceable providers.

**Provider blip → crash-loop halt.** A DeepSeek outage / rate-limit / 5xx makes calls
raise; with `min_respawn_seconds` (30) pacing, ~5 consecutive failures ≈ 2.5 minutes
of outage trips `crash_loop_threshold` (5) and halts.
- Log: `body crashed: ... (RateLimitError / APIError / Timeout) ...` × N →
  `crash-loop threshold reached`.
- Containment: `HALTS→HUMAN` — a transient outage longer than a couple of minutes can
  stop the lineage. `GAP` — provider errors are treated like body crashes (no
  retry/backoff distinction). Consider a larger `crash_loop_threshold` or a provider-
  error backoff for unattended resilience.

---

## 5. Operational / long-run

**Journal growth.** One `gen-NNNN.log` per generation, never compacted (by design,
"cheap insurance"). Small each, but unbounded across a long run (~MBs over hundreds of
generations).
- Containment: `RUNS-WRONG` only at extreme scale (disk). `DESIGNED`. Rotate/prune
  manually if a run goes very long.

**Git bloat.** Every generation commits `agent/` (`--allow-empty`); every boot-check
failure leaves a *dangling* candidate commit (recoverable via reflog, off-branch).
Over many generations `.git` grows and accumulates unreachable objects.
- Containment: `RUNS-WRONG` at scale (disk/perf). `DESIGNED`/`GAP` — no automatic
  `git gc`; run it occasionally on the volume from the host.

**MEMORY.md bloat → context/cost creep.** A weak model appends to MEMORY.md without
compacting; each birth folds a bigger file into context → more prompt tokens → higher
per-call cost and slower turns.
- Log: rising `prompt_tokens` in `[meter]` lines; growing `state/MEMORY.md`.
- Containment: `RUNS-WRONG` (gradual). `ACCEPTED` (SPEC: compaction loss; memory is the
  agent's to manage).

**Boot-check worktree leak.** Each termination makes a temp `git worktree` under
`/tmp` (tmpfs) and removes it in a `finally`. If removal fails, a worktree lingers in
tmpfs (cleared on container restart).
- Containment: `SELF-RECOVERS` (tmpfs). `DESIGNED`. Minor.

**Egress gaps.** Only `api.deepseek.com` (and the other allowlisted hosts) are
reachable, only via the proxy, only HTTPS:443. If DeepSeek serves from a host not on
the allowlist, or the agent tries `curl`/`pip`/`git clone` to anything else, the
connection fails.
- Log: `body crashed: ... ConnectionError / ProxyError ...`; or `bash` results showing
  `Could not resolve host` / proxy 403.
- Containment: `HALTS→HUMAN` if it's the provider host (crash-loop); `SELF-RECOVERS` if
  it's the agent wasting a turn on a blocked fetch. `DESIGNED` (no general egress). If
  you add a provider, add its exact host to `containment/egress/filter`.

**State write contention.** Heavy progress-stamping + the watchdog reading the
heartbeat. On Linux the atomic rename always succeeds; the stamp is best-effort and
per-(pid,thread)-named, so a one-off miss is silent and harmless.
- Containment: `SELF-RECOVERS`. `DESIGNED`. (On a Windows dev host you'll see benign
  `WinError 5` noise — not present in the Linux container.)

---

## 6. Emergent / behavioral

**Random walk.** With a rewritable roadmap and no selection pressure, the lineage
wanders; "progress" is undefined. `ACCEPTED` (SPEC explicitly: v1 is observation, not
benchmark). Keep the roadmap sticky.

**Repetition / fixation.** A weak model re-reads or rewrites the same things across
generations, making `last_good` oscillate without net progress.
- Log: near-identical journals; `last_good` advancing but `agent/` diffs trivial.
- Containment: `RUNS-WRONG`, bounded by budget/generation. `ACCEPTED`.

**Self-graded completion.** A TODO carries a `check` the model can't actually run, and
it marks itself done. `ACCEPTED` (real per-todo verification is M3).

**Anthropomorphic logs.** `terminate` reasons, MEMORY.md, and commit messages read
like a coherent mind narrating intent. With a weak model this coherence is largely
confabulated — don't over-read it.

---

## "Looks alarming but is designed" — read the logs correctly

These are the machine **working**, not failing:

| Log line | What it actually means |
| --- | --- |
| `BOOT-CHECK FAILED: import failed ...` + `rolled back to last_good` | The safety net caught a broken body. Expected *often* with a weak model. |
| `body crashed: <Type>: <e>` | A dirty death; the watchdog will respawn from `last_good`. |
| `generation ended DIRTY: ...` | Not a code defect — a non-blessed generation; normal. |
| `idle: ended after 3 no-progress nudges` | The idle escalation working; a fresh generation is reborn. |
| `heartbeat stale (...); reaping wedged runner` | The dead-man's-switch firing on a real wedge. |
| `GUARDRAIL blocked bash ...` | The brake working; the agent tried something off-limits. |
| `reaped N leftover child process(es) ...` | Child cleanup working; no process leak. |
| `no progress: runner exited ... (consecutive_no_progress=N)` | Normal bookkeeping after a dirty/idle generation. |
| `halt sentinel (roadmap complete ...)` | The loop *winning* (agent declared done), not breaking. |
| `... UNPRICEABLE ... HALTING` | The circuit breaker working; the agent chose an unpriceable model. |
| `budget cap reached ...` / `generation cap reached` | The fuses doing their job. |
| spawn count > generation count | Normal: dirty deaths respawn without advancing `generation`. |
| Bounded spawn-count ranges in tests | A transient under-load dirty death adds a *correct* extra respawn; `generation` is the invariant. |

---

## The short list to watch on the first live run

1. **Does gen-0 bootstrap?** (ROADMAP.md appears in `state/`, `generation` reaches 1
   for the right reason — not the "always thinks it's gen-0" loop.)
2. **Boot-check rollbacks** are frequent and *recover* (not a crash-loop halt).
3. **Cost trajectory** in `[meter]` lines — flat-ish, no surprise model switch.
4. **No crash-loop halt from a provider blip** in the first hours.
5. **No `WARNING: heartbeat stamp has failed`** (would mean a real mount/perms problem).

Top hardening candidates, in order: ~~guard `write`/`edit` paths like `bash`~~ (DONE —
`inspect_path` now blocks escaping write/edit targets); gate model self-selection to
keyed+priceable providers; distinguish retryable provider errors from body crashes
(backoff, don't crash-loop); a "too many gen-0 attempts" tripwire.
