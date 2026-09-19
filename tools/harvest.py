#!/usr/bin/env python3
"""Harvest a Kaizen lineage from its Docker named volume to a clean host folder.

The whole lineage — git history, journals, evolved body — lives on a Docker named
volume (default `containment_lineage`, mounted at /lineage in the container, stored
inside the Docker VM at /var/lib/docker/volumes/<name>/_data). This script reaches in
READ-ONLY and writes a tidy ./runs/<run-id>/ on the host:

    runs/<run-id>/
      journal/        the full journal set, copied out and renamed with the outcome
                      (gen-0003-DIRTY.log, gen-0006-GRACEFUL.log, ...)
      agent/          the evolved body (working tree at the final last_good)
      state/          MEMORY.md, ROADMAP.md, TODO.json, status.json, last_good, wake
      git/
        log.txt       git log --oneline --stat for the body repo
        commits.txt   <sha>\\t<subject> per commit (machine-readable)
        diffs/        one diff per generation commit (git show) — what each gen changed
      RUN_SUMMARY.md  one row per generation: outcome, cost, tokens, tool calls,
                      guardrail blocks, terminate reason, final commit
      INDEX.md        gen -> outcome -> one-line summary

Read-only over the volume: nothing about the agent or containment changes. Works on a
finished OR a running lineage (a read-only mount of an in-use volume is fine).

Usage (one command, run from the repo root):
    python tools/harvest.py
    python tools/harvest.py --volume containment_lineage --out ./runs
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

# The shell run inside a throwaway alpine/git container: copy the lineage out and
# generate the git report. `safe.directory` avoids git's dubious-ownership refusal
# (the repo is owned by the agent uid, we read it as root).
_CONTAINER_SCRIPT = r"""
set -e
G="git -c safe.directory=/lineage -C /lineage"
mkdir -p /out/journal /out/agent /out/state /out/git/diffs
cp -a /lineage/substrate/journal/. /out/journal/ 2>/dev/null || true
cp -a /lineage/agent/. /out/agent/ 2>/dev/null || true
cp -a /lineage/substrate/state/. /out/state/ 2>/dev/null || true
cp -a /lineage/substrate/state/last_good /out/git/LAST_GOOD.txt 2>/dev/null || true
$G log --oneline --stat --no-color > /out/git/log.txt 2>/dev/null \
    || echo "(no git history)" > /out/git/log.txt
$G log --format='%H%x09%s' --no-color > /out/git/commits.txt 2>/dev/null || true
$G log --format='%H%x09%s' --no-color 2>/dev/null | while IFS="$(printf '\t')" read -r sha subject; do
  gen=$(printf '%s' "$subject" | sed -n 's/^gen \([0-9][0-9]*\):.*/\1/p')
  short=$(printf '%s' "$sha" | cut -c1-8)
  if [ -n "$gen" ]; then
    fn=$(printf 'gen-%04d-%s.diff' "$gen" "$short")
  else
    fn="baseline-$short.diff"
  fi
  $G show --no-color "$sha" > "/out/git/diffs/$fn" 2>/dev/null || true
done
echo HARVEST_OK
"""

# -- journal parsing ---------------------------------------------------------

_RE_BIRTH = re.compile(r"=== generation (\d+) birth ===")
_RE_LASTGOOD = re.compile(r"checking out last_good (\w+)")
_RE_NOLASTGOOD = re.compile(r"no last_good yet")
# per-call meter line: "[meter] provider · model · $cost · ptok+ctok tok · lineage …"
_RE_METER = re.compile(r"· \$([\d.]+) ·")
_RE_METER_TOK = re.compile(r"(\d+)\+(\d+) tok")
# per-generation summary (written when a gen ends): the fallback for very old journals
_RE_COST = re.compile(r"gen \d+ cost \$([\d.]+) \((\d+) call\(s\), (\d+) tokens\)")
_RE_GRACEFUL = re.compile(r"generation \d+ graceful")
_RE_DIRTY = re.compile(r"generation ended DIRTY: (.+?) \(consecutive_dirty")
_RE_HALT_ROADMAP = re.compile(r"roadmap complete -> HALT sentinel")
_RE_BLESSED = re.compile(r"blessed last_good = (\w+)")
_RE_COMMITTED = re.compile(r"committed candidate (\w+)")
# match anywhere on the line (journal lines carry a "[HH:MM:SS] " prefix).
_RE_TOOLCALL = re.compile(r"· calls \S")
_RE_BLOCK = re.compile(r"GUARDRAIL blocked")


def _short(sha: str | None) -> str:
    return sha[:10] if sha else "—"


def parse_journal(path: Path, gen: int) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    # cost/tokens from the per-call [meter] lines (present live, mid-generation).
    meter = _RE_METER.findall(text)
    cost = sum(float(c) for c in meter)
    llm_calls = len(meter)
    tok_pairs = _RE_METER_TOK.findall(text)
    tokens = sum(int(a) + int(b) for a, b in tok_pairs)
    if not meter:  # very old journal: fall back to the per-generation summary line
        s = _RE_COST.findall(text)
        cost = sum(float(c) for c, _, _ in s)
        tokens = sum(int(t) for _, _, t in s)
        llm_calls = sum(int(m) for _, m, _ in s)
    elif not tok_pairs:  # [meter] without the token breakdown (older format)
        tokens = sum(int(t) for _, _, t in _RE_COST.findall(text))
    tool_calls = len(_RE_TOOLCALL.findall(text))
    blocks = len(_RE_BLOCK.findall(text))
    attempts = len(_RE_BIRTH.findall(text))

    lg = _RE_LASTGOOD.search(text)
    birth_ref = (
        _short(lg.group(1)) if lg else ("none" if _RE_NOLASTGOOD.search(text) else "—")
    )

    # final outcome = the LAST terminal event in the file (gens can respawn dirty
    # several times before a clean bless; the journal is appended each attempt).
    outcome, tag = "in-progress", "INPROGRESS"
    last_pos = -1
    if _RE_HALT_ROADMAP.search(text):
        m = list(_RE_HALT_ROADMAP.finditer(text))[-1]
        if m.start() > last_pos:
            outcome, tag, last_pos = "HALT (roadmap complete)", "HALT", m.start()
    for m in _RE_GRACEFUL.finditer(text):
        if m.start() > last_pos:
            outcome, tag, last_pos = "graceful", "GRACEFUL", m.start()
    for m in _RE_DIRTY.finditer(text):
        if m.start() > last_pos:
            outcome, tag, last_pos = f"dirty: {m.group(1)}", "DIRTY", m.start()

    blessed = _RE_BLESSED.findall(text)
    committed = _RE_COMMITTED.findall(text)
    final_commit = _short(
        blessed[-1] if blessed else (committed[-1] if committed else None)
    )

    return {
        "gen": gen,
        "attempts": attempts,
        "birth_ref": birth_ref,
        "outcome": outcome,
        "tag": tag,
        "cost": cost,
        "tokens": tokens,
        "llm_calls": llm_calls,
        "tool_calls": tool_calls,
        "blocks": blocks,
        "final_commit": final_commit,
    }


def load_commit_reasons(commits_txt: Path) -> dict[int, tuple[str, str]]:
    """gen -> (short_sha, reason) parsed from the commit subjects 'gen N: <reason>'."""
    out: dict[int, tuple[str, str]] = {}
    if not commits_txt.exists():
        return out
    for line in commits_txt.read_text(encoding="utf-8", errors="replace").splitlines():
        if "\t" not in line:
            continue
        sha, subject = line.split("\t", 1)
        m = re.match(r"gen (\d+): (.*)", subject)
        if m:
            out[int(m.group(1))] = (sha[:10], m.group(2).strip())
    return out


def _oneline(s: str, n: int = 100) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


# -- orchestration -----------------------------------------------------------


def run_container(volume: str, image: str, out_dir: Path) -> None:
    cmd = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{volume}:/lineage:ro",
        "-v",
        f"{out_dir.resolve()}:/out",
        "--entrypoint",
        "sh",
        image,
        "-c",
        _CONTAINER_SCRIPT,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if "HARVEST_OK" not in proc.stdout:
        sys.stderr.write(proc.stdout + "\n" + proc.stderr + "\n")
        raise SystemExit(
            f"harvest container failed (is the volume '{volume}' present? "
            f"`docker volume ls`). See output above."
        )


def build_reports(out_dir: Path) -> list[dict]:
    journal_dir = out_dir / "journal"
    reasons = load_commit_reasons(out_dir / "git" / "commits.txt")
    rows: list[dict] = []
    for jp in sorted(journal_dir.glob("gen-*.log")):
        m = re.search(r"gen-(\d+)", jp.name)
        if not m:
            continue
        row = parse_journal(jp, int(m.group(1)))
        # terminate reason: the agent's own words from the commit (graceful), else the
        # dirty reason.
        if row["gen"] in reasons:
            row["reason"] = reasons[row["gen"]][1]
            if row["final_commit"] in ("—", None):
                row["final_commit"] = reasons[row["gen"]][0]
        elif row["tag"] == "DIRTY":
            row["reason"] = row["outcome"].replace("dirty: ", "")
        else:
            row["reason"] = "—"
        rows.append(row)

    _write_summary(out_dir, rows)
    _write_index(out_dir, rows)
    _rename_journals(journal_dir, rows)
    return rows


def _write_summary(out_dir: Path, rows: list[dict]) -> None:
    lines = [
        "# Run summary",
        "",
        (
            f"Generations: {len(rows)} · "
            f"total cost: ${sum(r['cost'] for r in rows):.4f} · "
            f"total tokens: {sum(r['tokens'] for r in rows):,}"
        ),
        "",
        (
            "| gen | birth last_good | outcome | cost | tokens | tool calls | blocks | "
            "terminate reason | final commit |"
        ),
        "|----:|---|---|---:|---:|---:|---:|---|---|",
    ]
    for r in rows:
        att = f" (×{r['attempts']})" if r["attempts"] > 1 else ""
        lines.append(
            f"| {r['gen']}{att} | `{r['birth_ref']}` | {r['outcome']} | "
            f"${r['cost']:.4f} | {r['tokens']:,} | {r['tool_calls']} | {r['blocks']} | "
            f"{_oneline(r['reason'])} | `{r['final_commit']}` |"
        )
    lines += [
        "",
        (
            "_tool calls / blocks are recorded only when log_transcript was on for that "
            "run; older runs may show 0._"
        ),
        "",
    ]
    (out_dir / "RUN_SUMMARY.md").write_text("\n".join(lines), encoding="utf-8")


def _write_index(out_dir: Path, rows: list[dict]) -> None:
    lines = ["# Journal index", ""]
    for r in rows:
        name = f"gen-{r['gen']:04d}-{r['tag']}.log"
        lines.append(
            f"- **gen {r['gen']}** → **{r['outcome']}** — {_oneline(r['reason'], 160)}"
        )
        lines.append(
            f"  - journal: `journal/{name}` · diff: `git/diffs/gen-{r['gen']:04d}-*.diff`"
        )
    (out_dir / "INDEX.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _rename_journals(journal_dir: Path, rows: list[dict]) -> None:
    for r in rows:
        src = journal_dir / f"gen-{r['gen']:04d}.log"
        if src.exists():
            src.rename(journal_dir / f"gen-{r['gen']:04d}-{r['tag']}.log")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Harvest a Kaizen lineage from its volume.")
    p.add_argument(
        "--volume", default="containment_lineage", help="Docker volume name."
    )
    p.add_argument("--out", default="./runs", help="Host output base directory.")
    p.add_argument(
        "--image", default="alpine/git", help="Image with git (for the report)."
    )
    p.add_argument("--run-id", default=None, help="Override the run folder name.")
    args = p.parse_args(argv)

    run_id = args.run_id or "run-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out) / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[harvest] reading volume '{args.volume}' -> {out_dir} …")
    run_container(args.volume, args.image, out_dir)
    rows = build_reports(out_dir)

    last_good = out_dir / "git" / "LAST_GOOD.txt"
    lg = (
        last_good.read_text(encoding="utf-8").strip()[:10]
        if last_good.exists()
        else "—"
    )
    print(f"[harvest] done. {len(rows)} generation(s); final last_good = {lg}")
    print(f"[harvest] open: {out_dir / 'RUN_SUMMARY.md'}")
    print(f"[harvest]       {out_dir / 'INDEX.md'}")
    print(f"[harvest]       {out_dir / 'git' / 'log.txt'}  +  git/diffs/*.diff")
    # a compact echo so you see it without opening a file
    for r in rows:
        print(
            f"    gen {r['gen']:<2} {r['tag']:<10} ${r['cost']:.4f}  "
            f"{r['tokens']:>7,} tok  {r['tool_calls']:>3} tools  "
            f"{r['blocks']} blocks  {_oneline(r['reason'], 70)}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
