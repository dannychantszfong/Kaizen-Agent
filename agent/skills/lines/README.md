# lines — count lines of code per file extension

Summarize a codebase by counting lines and files grouped by extension. Useful
when the agent needs a quick sense of a repo's size and language mix before
diving in.

## Usage

Run it with `bash`:

```bash
python skills/lines/count.py [PATH]
```

- `PATH` — directory to scan (recursively). Defaults to the current directory.

It skips common noise directories (`.git`, `.venv`, `__pycache__`, `node_modules`,
`.pytest_cache`) and any file it can't read as text.

## Example

```bash
$ python skills/lines/count.py src
ext           files    lines
.py              11      667
.md               1       21
----------------------------
total            12      688
```

## When to use it

Reach for `lines` instead of hand-rolling a `find | wc -l` pipeline when you want
a per-extension breakdown with the noise directories already excluded.
