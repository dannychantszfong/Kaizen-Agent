# skills/

A **skill** is a directory containing a `README.md` that documents a CLI tool (or
a procedure) the agent runs through `bash`. This is capability-as-documentation:
there is no plugin protocol and no import hook. Discovery just finds the
directories; the model reads a skill's README and invokes it with the `bash` tool
like a developer would.

## How it works

`spine.skills.discover_skills(root)` returns every subdirectory of `root` that
contains a `README.md`, and `spine.skills.skills_prompt(...)` renders a short
catalogue for the system prompt. A clone wires it in at startup:

```python
from spine import Agent, default_tools
from spine.skills import discover_skills, skills_prompt

skills = discover_skills("skills")
prompt = "<your base prompt>\n\n" + skills_prompt(skills)   # empty string if none

agent = Agent(model="anthropic/claude-opus-4-8", tools=default_tools(), system_prompt=prompt)
```

The agent then sees, in its prompt, that `lines` exists and where it lives. When a
task calls for it, the agent `read`s `skills/lines/README.md` and runs the
documented command via `bash`. The loop and the four tools are untouched.

## Adding a skill

1. Make a directory under `skills/` (e.g. `skills/my-skill/`).
2. Drop in a `README.md` whose **first non-empty line** is a one-line summary —
   that line becomes the catalogue description.
3. Put any script(s) the README tells the agent to run alongside it.

See [`lines/`](lines/) for a worked example.
