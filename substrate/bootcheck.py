"""substrate/bootcheck.py — does a candidate body import and survive a smoke run?

Run as a subprocess by the runner against a *clean checkout* of the committed
body (a temp git worktree), so it judges exactly what rebirth would load — not the
dirty working tree the agent just edited. Untracked or gitignored files are
therefore correctly excluded; a body that smoke-passes only because of files git
won't carry forward will fail here, as it should.

Exits 0 on success, non-zero on any import error, exception, or crash. Kept tiny
and dependency-light on purpose: this is a gate, not a test suite. The body's own
tests are advisory (SPEC invariant #3).
"""

from __future__ import annotations

import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: bootcheck.py <agent_src_dir>", file=sys.stderr)
        return 2

    agent_src = Path(argv[0]).resolve()
    sys.path.insert(0, str(agent_src))

    # 1. The body must import. An import-time failure is a dead body.
    try:
        import spine  # noqa: F401
        from spine import Agent, default_tools
        from spine.provider import Completion
    except BaseException as e:  # noqa: BLE001 - any failure means "won't boot"
        print(f"import failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    # 2. Smoke run: one offline turn through the real loop — no tools, no network.
    def _complete(model, messages, tools=None):
        return Completion(content="boot smoke ok")

    try:
        agent = Agent(model="bootcheck", tools=default_tools(), complete=_complete)
        out = agent.run("boot check: reply and stop.")
    except BaseException as e:  # noqa: BLE001
        print(f"smoke run failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(f"boot ok: {out!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
