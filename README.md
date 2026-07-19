# spine

A minimal Python agent backbone: a language model, four tools, and a loop. It's a
**foundation to build on** — you clone it and grow it into the agent you actually
want, rather than running it as a finished product. If you're reading this in a
fresh clone, this *is* your agent's repo; the next step is to shape it into yours.

The design rationale and full spec live in [`doc/spine-spec.md`](doc/spine-spec.md);
a code-level walkthrough is in [`doc/ARCHITECTURE.md`](doc/ARCHITECTURE.md); the
always-on orientation for coding agents is in [`CLAUDE.md`](CLAUDE.md) and
[`AGENTS.md`](AGENTS.md).

## What's here

```
src/spine/
  provider.py     # rented litellm wrapper: complete(model, messages, tools)
  agent.py        # the loop, conversation state, tool dispatch, hooks
  tools/          # read · write · edit · bash  (the only four)
  hooks.py        # before/after_tool_call + session lifecycle; ships PERMISSIVE
  skills.py       # skill discovery/loading seam
  prompts/system.md
main.py           # runnable entry point — the repo is the agent, so it lives at the root
skills/           # capability-as-documentation; `lines/` is a worked example
tests/
```

The model gets exactly four tools — `read`, `write`, `edit`, `bash` — and `bash`
is the escape hatch for everything else. The loop is hand-written and ~150 lines;
read it top to bottom in `src/spine/agent.py`. The whole core is small enough to
hold in your head, which is the point.

## Install

```bash
pip install -e ".[dev]"      # or: uv sync --extra dev
```

## Run it

`litellm` reads provider credentials from the environment. For Anthropic:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python main.py "list the python files here and count their lines"
```

Pick any litellm-supported model with `--model`:

```bash
python main.py --model gpt-4o "summarize README.md"
```

## Run the tests

No API key needed — the loop test drives a stubbed provider, so nothing hits the
network.

```bash
pytest
```

## Make it your agent

This repo is the starting skeleton; growing it into a real agent is the expected
next step, and it happens at the seams — the loop and the four tools usually stay
as they are:

1. Add domain tools under `src/spine/tools/` (implement the `Tool` protocol) and
   register them with `Agent(tools=[...])`.
2. Edit `src/spine/prompts/system.md` for the domain and voice.
3. Drop in skills (CLI tools + READMEs) under `skills/`.
4. Tighten the `before_tool_call` hook if the agent needs policy (e.g. constrain
   `bash`).
5. Run.

See [`doc/spine-spec.md`](doc/spine-spec.md) for the reasoning, and
[`CLAUDE.md`](CLAUDE.md) for the bootstrap protocol a coding agent should follow on
a fresh clone: read the skeleton → interview the developer → discuss → build.

## More

- [`doc/ARCHITECTURE.md`](doc/ARCHITECTURE.md) — code-level walkthrough with the
  extension recipes (add a tool, a hook, a skill, swap the provider).
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — the grain to keep while you build on it.
- [`skills/`](skills/) — what a skill is, and a runnable `lines` example.

---
To turn it on — restart (this also brings in every recent fix)
A restart is needed because the code runs from the volume (which still holds the older copy). The fresh path gets you all of it at once — full transcript + live journal, context-window fit, transient-error retry, and restart-on-halt:


docker compose --env-file .env -f containment/docker-compose.yml down -v
docker compose --env-file .env -f containment/docker-compose.yml up --build
(Or, to keep the current lineage, the in-place docker run … cp … refresh from earlier, then up -d.)

Reading / saving it live

# follow live
docker compose --env-file .env -f containment/docker-compose.yml logs -f kaizen
# the per-generation journals are now complete + live on the volume:
docker run --rm -v containment_lineage:/lineage -v "${PWD}:/out" alpine cp -r /lineage/substrate/journal /out/journal
That's the full "watch everything it says, thinks, and does" pass. If anything in the stream is still less detailed than you want (e.g. you'd like per-call latency, or the raw request JSON), say the word and I'll add it.