# Kaizen containment — the box a lineage lives inside

> SPEC invariant #1: *Kaizen runs and executes arbitrary code unsupervised;
> isolation protects the host and gives clean resets.* This document is how that
> isolation is built, what it does and does not guarantee, and how to run and
> prove it. The artifacts live in [`containment/`](../containment/).

## What we are defending against

The body (`agent/`) is the smart, **untrusted** thing. Each generation it runs an
LLM with a shell (`bash`) and writes its own code. We assume it may, by accident or
drift: try to escape `agent/`, try to edit its own brakes, try to kill or confuse
its supervisor, try to reach the network, or simply crash dirty. Containment makes
the dangerous versions of those *impossible at the OS level*, not merely discouraged
by a hook the body could rewrite.

The guardrail hook (`substrate/guardrail.py`) is still here, and still useful — it
makes the common, accidental escapes loud and recoverable in-process. But it is
**defense-in-depth**, not the wall. It works on the `bash` command string and a body
that rewrites its own loop can skip it. The wall is this container.

## The two boundaries

Containment is two OS mechanisms doing two different jobs. Keeping them distinct is
the whole design.

### 1. Immortal **code** is read-only to the agent (the primary boundary)

Everything under `/lineage` is `root:root` and `r-x` (dirs `0555`, files `0444`) —
*except* the writable carve-outs below. So the agent uid literally cannot modify:

- `watchdog.py`, `runner.py` — the supervisor and the lifecycle (invariant #2),
- `substrate/*.py` — the guardrail, config, state store, metering, boot-check
  (invariant #5),

…not with `write`/`edit`, not with `bash`, not even with `git checkout` (git would
have to rewrite a read-only working-tree file, which fails). This is the **primary
agent↔substrate boundary**, and it is a hard wall: the runner only ever *reads and
executes* this code, so making it read-only to the agent costs the runner nothing.

### 2. Runtime **state** is agent-writable data on a durable volume

The runner must persist the lineage as it runs (invariant #4). So these are
`agent`-owned and writable, and they live on a mounted **named volume** so they
survive `docker rm`:

- `agent/` — the body's editable tree (the only place it writes code),
- `substrate/state/` — status, `last_good`, `wake`, heartbeat, and the canonical
  `MEMORY.md` / `ROADMAP.md` / `TODO.json`,
- `substrate/journal/` — the per-generation logs,
- `.git` — the body's local history (commits are **local only**; see *Network*).

This is *data*, not brakes. It is supposed to be writable; that is invariant #4.

> **Why state can't also be read-only to the agent.** The body runs *in-process*
> inside the runner (a deliberate M0/M1 choice: one PID to watch). One process means
> one uid, and that uid must write state — so OS permissions cannot distinguish "the
> runner writing `last_good`" from "the body writing `last_good`". We close the lazy
> path (the guardrail blocks `bash` from naming `substrate/`, and mirrors carry-over
> through the trusted layer), and we accept a bounded residual risk (below). The
> immortal *code* — the part that actually matters for invariants #2/#5 — is fully
> protected regardless, because the runner never writes it.

## The principals (who runs as whom)

| Process | uid | Why |
| --- | --- | --- |
| `watchdog.py` | **root** | The immortal supervisor. A *different* uid from the agent, so the body (agent uid) cannot `kill`/`ptrace` it — invariant #2 becomes an OS fact, not a hook's promise. Root is required to drop privilege to `agent` per spawn. |
| `runner.py` + the in-process body | **agent** (uid 1000) | The watchdog spawns it via `gosu agent` (`KAIZEN_RUNNER_PREFIX="gosu agent"`). Its only writable paths are the carve-outs above. |
| `bootcheck.py` | agent | A subprocess of the runner; runs in a throwaway git worktree under `/tmp`. |

The privilege drop is the substrate's decision, carried in `KAIZEN_RUNNER_PREFIX`,
which the watchdog reads and the agent can never set (invariant #6). On a dev host
the prefix is empty and everything runs as the developer — the container is where
the uid split becomes real.

## Network — egress default-deny

- `kaizen` is attached to a Docker network declared `internal: true`: **no route to
  the internet**. Its only reachable peer is the egress proxy.
- `egress-proxy` (tinyproxy) bridges the internal network and the outside, and
  forwards **only** HTTPS `CONNECT` to a hard **named-domain allowlist**
  ([`containment/egress/filter`](../containment/egress/filter)), denying everything
  else (`FilterDefaultDeny Yes`, `ConnectPort 443`). Each entry is one FQDN anchored
  `^…$` — never a wildcard or allow-all. Enabled by default: `api.anthropic.com`,
  `api.openai.com`, `generativelanguage.googleapis.com` (Gemini AI Studio),
  `api.deepseek.com`, `openrouter.ai`. Add a provider by adding one anchored line.
- The body reaches a provider because `HTTPS_PROXY` points at the proxy; it can
  reach nothing else. Because the agent may switch models/providers across
  generations, the allowlist is the set of providers you permit — the meter prices
  whichever it actually used (see *Provider-aware metering*).
- **The only secrets in the box are the provider API keys** — `ANTHROPIC_API_KEY`,
  `OPENAI_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY` —
  each passed from host env and defaulting to empty. Set only the ones you want;
  the rest are simply unavailable (a call to an unkeyed provider fails that
  generation, no crash). There are **no VCS credentials**: per-generation commits
  are local-only and there is no `push` anywhere in the code. You move the lineage
  off the box yourself, from the host: `git -C <volume>/ ...` or `docker cp`.

## Container hardening

From [`containment/docker-compose.yml`](../containment/docker-compose.yml):

- `read_only: true` root filesystem; the only writable surfaces are the `lineage`
  volume and a `tmpfs` at `/tmp` (boot-check worktrees, caches; `HOME`/`XDG_*` point
  there so a read-only `/` is enough).
- `cap_drop: [ALL]`, then `cap_add: [SETUID, SETGID, DAC_OVERRIDE]` — the *only*
  capabilities added back, and all for the **trusted root supervisor**: SETUID/SETGID
  for `gosu`'s root→agent drop, and DAC_OVERRIDE so the root watchdog can write the
  agent-owned lineage state (with caps dropped, root is no longer exempt from
  file-permission checks). The agent gains none of these — after the `gosu` drop it is
  a non-root uid with its capabilities cleared and `no-new-privileges` set, so the
  immortal-code boundary holds.
- `security_opt: [no-new-privileges:true]` — a dropped-privilege child can never
  regain privilege via a setuid bit.
- **No `--privileged`. No docker socket mounted.** The box cannot reach the host
  daemon or escalate to the host.
- **No `--privileged`. No docker socket mounted.** The box cannot reach the host
  daemon or escalate to the host. *(Aside: an image CVE scanner will flag the
  `python:3.12-slim` base — rebuild on a patched base periodically; it does not
  affect the isolation model.)*
- Dependencies (`pydantic`, `litellm`, `psutil`) are baked at build time; nothing is
  installed at runtime (the network wouldn't allow it anyway). litellm reaches all
  five enabled providers natively — none need an extra SDK baked in. `psutil` is the
  substrate's process-tree liveness observation + child reaping (see *Liveness*).

## Provider-aware metering (the budget circuit breaker)

The budget cap is only honest if it sees spend on **every** provider, and the agent
may switch model — and thus provider — across generations (its choice lives in the
body at `agent/MODEL`; absent, the default `deepseek/deepseek-chat` applies — very
cheap, capable, runs on `DEEPSEEK_API_KEY` alone, and priced from litellm's cost map
via `cost_per_token`, so the breaker does not false-trip on it).

So metering ([`substrate/metering.py`](../substrate/metering.py)) is
provider-agnostic: it prices the **actual model that ran** off the real litellm
response, sums all spend across all providers into the **one** running total against
the **one** `$cap`, and bills it on **every** exit path — including dirty deaths
(a body that burned tokens then crashed still spent the money). Each call prints
`provider · model · $cost · lineage $running / $cap` to the journal.

The breaker: if litellm **cannot price** a model that ran (the pricing call raises,
or returns `$0` despite real token usage), the meter is blind there — and a blind
meter is a broken cap. Rather than silently bill `$0`, the runner **halts the
lineage** (sets the halt flag *and* writes the halt sentinel; the watchdog stops).
Pick models litellm prices, or the lineage will stop rather than spend unwatched.

## Liveness (substrate-observed, never agent-reported)

Two failures need two mechanisms, plus progress tracking that keeps the backstop
from firing on real work. All of it is substrate-OBSERVED — a self-modifying agent
must not be able to disable or fake its own liveness, so no agent-callable tool can
stamp progress.

- **Progress-based liveness.** The substrate stamps a "last-progress" timestamp (the
  heartbeat) on each observed unit of work: an LLM call returning (in
  [metering](../substrate/metering.py)) and an executed tool call (in the guardrail
  hook). Frequent and multi-threaded, so the stamp is best-effort and uses a
  per-(pid,thread) temp name.
- **Process-tree observation.** [`LivenessMonitor`](../substrate/liveness.py) watches
  the runner's process *subtree* from outside (psutil) and refreshes last-progress
  while any child is actively consuming CPU — so a long legitimate child (a build, a
  test run, a server the agent started) does NOT get the parent reaped. Observation,
  not cooperation: the agent instruments nothing.
- **Event-based idle nudge.** A generation is a *loop of turns*. If the agent ends a
  turn without terminating, the substrate injects a short nudge ("continue or
  terminate") and loops — purely at the stopped-without-terminate boundary, so
  thinking-model latency never trips it. After `idle_nudge_limit` (3) consecutive
  no-progress nudges, the generation ends gracefully and a fresh one is reborn (the
  watchdog's crash-loop cap catches a lineage that only idles).
- **Generous hard backstop.** The watchdog reaps a runner whose last-progress is
  older than `heartbeat_timeout_seconds` — now a dead-man's-switch for a true wedge
  (zero progress AND no active child for the whole window), set to **30 minutes**,
  not a work-time limit. On a reap it kills the whole subtree; after every generation
  it sweeps the runner's process group (POSIX) so no child leaks across lives.
- **Pacing.** `min_respawn_seconds` = **30** paces respawns; `budget_cap_usd` (**100**)
  and `max_generations` (**1000**) are loose-but-finite runaway fuses. The turn loop
  also self-bounds on the budget, since the watchdog only checks caps between
  generations. The real spend limit is the key's funding + the provider-side cap.

## How the lineage persists

The image bakes a pristine `/lineage` (a fresh git history with one "immortal seed"
commit, the permission boundary already applied). On first `up`, the empty named
volume initializes from that image content **including ownership**. On every later
`up` the volume is reused — so the lineage (evolved `agent/`, accumulated state, git
history) **survives `docker rm` / `docker compose down`** and resumes.

Consequence, stated plainly: **updating the immortal code requires rebuilding the
image *and* resetting the volume** (`docker volume rm containment_lineage`), because
the entrypoint never overwrites an initialized volume. That is deliberate — the
volume is the experiment's durable home, not something an image rebuild should
silently mutate. A future hardening could re-sync immortal code from the image on
each boot; v1 keeps the entrypoint dumb.

## Invariant → mechanism

| Invariant | Enforced by |
| --- | --- |
| #1 sandbox | the container itself: read-only rootfs, dropped caps, no host daemon access, egress default-deny |
| #2 watchdog outside `agent/`, unreachable | watchdog is a **root** process; its code is root-owned read-only; agent uid cannot signal a root process |
| #3 boot-check + `last_good` always exists | unchanged from M0: the runner boot-checks a clean worktree checkout before blessing; image seeds an initial `last_good` |
| #4 state persists continuously | carry-over mirrored to `state/` after every mutating tool call and at `session_end`; state is agent-writable and on a durable volume |
| #5 agent can't edit its brakes | guardrail/runner/watchdog code is root-owned `0555`; the in-process guardrail hook is inner defense-in-depth |
| #6 scheduling/PID authority in substrate | the agent only writes a `wake` note (data); the **root** watchdog owns PIDs, killing, and the privilege-drop prefix |

## Accepted residual risks (v1, conscious)

- **A hook-bypassing body can corrupt its *own* lineage state.** Because body and
  runner share a uid, a body that rewrites its loop to skip the guardrail could write
  `substrate/state/` (e.g. garble `last_good`). Blast radius is its *own continuity*:
  worst case it self-halts (a bad `last_good` → crash → the watchdog's crash-loop cap
  stops the lineage), which is recoverable by the operator and observable in the
  journal. It **cannot** reach the host, the watchdog, or the immortal code. A future
  hardening: have the watchdog keep an out-of-band copy of `last_good`.
- **Whole-tree `git reset --hard` on rollback** assumes the immortal tree is
  immutable (so the reset never needs to rewrite a read-only file). That assumption
  is *guaranteed* by boundary #1, so the two reinforce each other.
- **The watchdog runs as root** inside the container. It is small, boring, baked
  read-only, and makes no network calls; the container as a whole cannot escalate to
  the host. Acceptable for v1, documented here so the choice is conscious.

## Build, run, prove

```sh
# 1. Provide at least the key for the default model's provider (Anthropic). Add any
#    others you want the agent to be able to switch to; unset ones stay unavailable.
export ANTHROPIC_API_KEY=sk-ant-...
# export OPENAI_API_KEY=...  GEMINI_API_KEY=...  DEEPSEEK_API_KEY=...  OPENROUTER_API_KEY=...

# 2. Build + launch the lineage (watchdog -> runner -> body, generation after gen).
#    There is no PRIME DIRECTIVE to fill — generation zero authors its own purpose.
docker compose -f containment/docker-compose.yml up --build

# Inspect the lineage from the host (no push happens inside the box):
docker run --rm -v containment_lineage:/lineage alpine \
  sh -c 'cat /lineage/substrate/state/MEMORY.md; git -C /lineage log --oneline'
```

Acceptance tests (`tests/test_m1_containment.py`):

```sh
# Always runs (CLI only, no daemon): the compose topology is valid + shaped right.
pytest tests/test_m1_containment.py::test_compose_config_is_valid

# Opt-in, needs a Docker daemon: builds the image and proves the boundary —
# the agent uid can write agent/ and state/, but NOT runner.py/watchdog.py/
# substrate/*.py or the host rootfs; and state survives container removal.
KAIZEN_CONTAINER_TESTS=1 pytest tests/test_m1_containment.py
```

## STOP — before the first live generation

M1 is proven **without real spend** (the wiring tests run on injected/stubbed
providers; the container tests use `gosu`/volumes, not the LLM). Do not launch a
real gen-0 from here. The operator:

1. exports the provider key(s) — at minimum `ANTHROPIC_API_KEY` for the default
   model,
2. runs `docker compose ... up --build`, and watches the cost the runner prints —
   per call (`[meter] provider · model · $cost · lineage $running / $cap`) and per
   generation (`gen N cost $X … lineage total $Y of $cap`).

Generation zero chooses the lineage's purpose itself; there is nothing to fill in
first.
