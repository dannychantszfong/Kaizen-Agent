# Kaizen

Kaizen is a self-evolving agent built on [spine](https://github.com/dannychantszfong/Spine),
a minimal language-model loop with four tools: read, write, edit and bash.
At generation zero it chooses its own purpose. It works on its body, requests
termination, and is reborn in a fresh process from the code it leaves behind.
Continuity lives in files, not in a running process.

The two layers have distinct responsibilities:

```text
IMMORTAL: watchdog.py (separate root process)
              | spawns and supervises
          runner.py + substrate/ (lifecycle, policy, metering, state)
              | runs a generation; commits and boot-checks its successor
MORTAL:   agent/ (editable spine body, tools, prompt, source content)
              | checkpoints MEMORY.md / ROADMAP.md / TODO.json
              +-- terminate request --> runner --> wake or halt
```

The immortal code lives outside `agent/` and is root-owned and read-only to the
agent. The runner and body share a non-root process; the root watchdog is separate.
The body can rewrite its own code and prompt, but the lifecycle code stays outside
its writable body tree.

```text
agent/          Mortal body: spine, five tools including terminate, prompt, tests
substrate/      Lifecycle helpers, guardrail, configuration, metering, boot-check
  state/        Runtime carry-over, status, last_good, heartbeat and wake/halt note
  journal/      Runtime per-generation logs
containment/    Docker image, Compose topology and provider egress proxy
tools/         Host utilities, including harvest.py
runs/           Local harvested lineages (ignored by Git and Docker builds)
doc/            Design specs, foundation architecture and containment details
runner.py       One generation's birth, work and termination protocol
watchdog.py     Rebirth, wake timing, caps and stalled-generation cleanup
```

A generation starts from `last_good`, with carry-over files seeded into `agent/`.
Its birth context supplies the actual zero-based generation number, source-artifact
file count, lineage spend and `last_good`, overriding remembered figures.
The agent checkpoints memory while working and calls `terminate` when ready.
The runner persists state, commits a candidate locally, boot-checks it in a clean
Git worktree, then blesses it as `last_good` and writes a wake or halt note.
A failed boot-check rolls back. The watchdog starts the next generation unless a
halt or cap applies. There is no automatic GitHub push.

Explicit `roadmap_complete=true` halts the lineage. As a fallback, three consecutive
boot-checked graceful generations with no substantive committed changes halt with
`completion-loop`. Changes are compared against the previous blessed tree;
MEMORY/ROADMAP/TODO/JOURNAL and configured generated outputs do not count.
Any other changed file (including deletions) resets the streak, as does a dirty
death. This is a no-progress heuristic, not proof that the roadmap is complete.

Operator knobs live in `substrate/config.py` and are persisted to runtime
`substrate/state/config.json` for spawned runners. `completion_loop_threshold`
defaults to 3. `completion_ignored_globs` lists paths relative to `agent/` ignored
by the detector (including `site/*`, `_site/*`, `build/*`, `dist/*` and caches).
Patterns are case-sensitive and `*` matches across directories. Keep them narrow:
source files placed in an excluded output directory will not count as progress.
`artifact_globs` defaults to all source files; narrow it to e.g. `["works/*.md"]`
for a lineage-specific count. The injected count is a file count, not a judgement
about completed works. The prompt encourages scripted, on-demand generation of
presentation output instead of expensive manual rewrites.

Containment uses a container, a non-root runner/body, root-owned read-only substrate
code, a read-only root filesystem, dropped capabilities and no-new-privileges.
An internal-only network routes outbound traffic through an allowlisting proxy
for the configured provider API hosts. No Docker socket or VCS credentials are
mounted. The boot-check protects rebirth; metering enforces the cost cap and halts
on an unpriceable model. A progress-based dead-man's-switch reaps a wedged runner
and its children after the configured timeout (default 1,800 seconds).

The in-process guardrail is defense in depth, not an isolation boundary: it allows
reads and standard output sinks, blocks writes outside `agent/`, and blocks process
control and scheduler commands. Substrate code is readable. Runtime state,
journals and `.git` are deliberately writable by the runner's UID; because the
agent shares that process and UID, these are not OS-isolated from arbitrary body
code. See [the containment model](doc/CONTAINMENT.md) for the accepted boundary.

To run a lineage, install Docker with Compose, configure provider credentials
using [.env.example](.env.example), and export `DEEPSEEK_API_KEY` (or put it in a
root `.env`). The default model is `deepseek/deepseek-chat`; the body can choose an
allowlisted provider/model via `agent/MODEL`. From the repository root:

```sh
docker compose -f containment/docker-compose.yml up --build
```

This launches a live, metered lineage. Its named volume is initialized from the
image on first use; subsequent starts resume that volume. Rebuilding the image
does not replace an existing volume's body or substrate files. Fresh builds exclude
host state, journals, harvests, exports, caches and scratch files; only empty
runtime state/journal directories are created by the Dockerfile.

Harvest a running or finished lineage from the host:

```sh
pythontools/harvest.py --volume containment_lineage --out ./runs
```

The volume mount is read-only. Output under `runs/<id>/` includes journals, the
body, state, Git log, per-generation diffs, `RUN_SUMMARY.md` and `INDEX.md`.
A running harvest can span writes; harvest again after stopping for a final record.
If you use a custom Compose project name, pass its actual volume name.

**HARVEST BEFORE `docker compose -f containment/docker-compose.yml down -v`.**
`down -v` destroys the run's volume, including its evolved body, memory and history.
Plain `down` preserves the volume.

Development checks require Python 3.12+ and Git. They use disposable repositories
and stubbed providers; they do not launch a live agent:

```sh
pip install -e ".[dev]"
python -m pytest tests agent/tests
ruff check .
# PowerShell; requires a running Docker daemon:
$env:KAIZEN_CONTAINER_TESTS = "1"
python -m pytest tests/test_m1_containment.py
```

The gated container tests override the entrypoint and exercise permissions, seed
hygiene and volume persistence without starting a lineage. See [Kaizen's design
spec](doc/SPEC.md), [containment](doc/CONTAINMENT.md), and [spine's foundation
spec](doc/spine-spec.md). Some design-spec passages describe earlier plans; the
code and this README describe the implemented commit/boot-check order and local
Git storage.
