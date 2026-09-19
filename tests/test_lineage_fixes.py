"""Regression proofs for completion, birth facts, guardrails and clean seeds."""

import subprocess
from types import SimpleNamespace

import pytest

from runner import Exit, Runner
from substrate.body import run_broken_body
from substrate.config import Config
from substrate.guardrail import SubstrateHooks, inspect_bash_command, inspect_path
from substrate.state_store import StateStore, Status
from watchdog import Watchdog


def setup(config):
    store = StateStore(config)
    store.ensure_dirs()
    sha = subprocess.check_output(
        ["git", "-C", str(config.root), "rev-parse", "HEAD"], text=True
    ).strip()
    store.write_last_good(sha)
    return store


def churn(ctx):
    for name in (
        "MEMORY.md",
        "ROADMAP.md",
        "TODO.json",
        "JOURNAL.md",
        "site/index.html",
    ):
        path = ctx.config.agent_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(ctx.generation), encoding="utf-8")
    ctx.sink.requested = True
    ctx.sink.reason = "final verification"


def test_completion_loop_halts_after_three_graceful_generations(lineage_repo):
    cfg = Config(root=lineage_repo)
    store = setup(cfg)
    logs = []
    for n in range(3):
        assert (
            Runner(cfg, body_runner=churn, log=logs.append).run_one_generation()
            == Exit.GRACEFUL
        )
        assert store.load_status().consecutive_no_substance == n + 1
        assert store.load_status().halted == (n == 2)
    assert store.load_status().halt_reason == "completion-loop"
    assert store.read_wake().halt
    assert "completion-loop" in Watchdog(cfg)._check_caps()
    assert any("completion-loop -> HALT" in line for line in logs)


def test_source_changes_and_dirty_deaths_reset_completion_streak(lineage_repo):
    cfg = Config(root=lineage_repo)
    store = setup(cfg)
    for _ in range(2):
        Runner(cfg, body_runner=churn).run_one_generation()

    def source(ctx):
        churn(ctx)
        (cfg.agent_dir / "original.html").write_text(
            "substantive original", encoding="utf-8"
        )

    Runner(cfg, body_runner=source).run_one_generation()
    assert store.load_status().consecutive_no_substance == 0
    Runner(cfg, body_runner=churn).run_one_generation()
    assert store.load_status().consecutive_no_substance == 1
    Runner(cfg, body_runner=run_broken_body).run_one_generation()
    assert store.load_status().consecutive_no_substance == 0
    assert not store.load_status().halted


def test_completion_configuration_survives_runner_reload(lineage_repo):
    cfg = Config(
        root=lineage_repo,
        completion_loop_threshold=2,
        completion_ignored_globs=("custom-output/*",),
        artifact_globs=("works/*.md",),
    )
    cfg.save()
    loaded = Config.load(lineage_repo)
    assert loaded.completion_loop_threshold == 2
    assert tuple(loaded.completion_ignored_globs) == ("custom-output/*",)
    assert tuple(loaded.artifact_globs) == ("works/*.md",)
    with pytest.raises(ValueError):
        Config(root=lineage_repo, completion_loop_threshold=0)


def test_birth_prompt_has_disk_ground_truth_despite_false_memory(lineage_repo):
    from spine.provider import Completion, ToolCall

    cfg = Config(root=lineage_repo, artifact_globs=("works/*.md",))
    store = setup(cfg)
    store.save_status(Status(generation=69, budget_spent_usd=12.5))
    store.flush_carryover(memory="Generation 28; 999 works")
    works = cfg.agent_dir / "works"
    works.mkdir()
    for name in ("first.md", "second.md"):
        (works / name).write_text("original", encoding="utf-8")
    captured = []

    def factory(config, meter):
        def complete(model, messages, tools=None):
            captured.extend(dict(m) for m in messages)
            return Completion(
                content="done",
                tool_calls=[
                    ToolCall(id="end", name="terminate", arguments={"reason": "test"})
                ],
            )

        return complete

    last_good = store.read_last_good()
    assert Runner(cfg, provider_factory=factory).run_one_generation() == Exit.GRACEFUL
    birth = next(m["content"] for m in captured if m["role"] == "user")
    assert birth.startswith("[Substrate-provided ground truth")
    assert "Generation (zero-based, blessed lives): 69" in birth
    assert "Source artifact files on disk: 2" in birth
    assert "12.500000" in birth
    assert last_good in birth
    assert "999" not in birth
    system = captured[0]["content"]
    assert "Trust these figures over numbers in MEMORY.md" in system
    assert "build script via bash" in system


@pytest.mark.parametrize(
    "command",
    [
        "cat ../runner.py",
        "head /etc/passwd",
        "rg hello ../substrate",
        "cat < /etc/passwd",
        "cat /etc/passwd > local.txt",
        "echo done >/dev/null",
        "echo done 2>/dev/stderr",
        "echo done > /dev/stdout",
        "echo done > /dev/null 2>&1",
        "cat /etc/passwd | wc -l",
        "echo x > test_runner.py",
        "touch test_watchdog.py",
        "touch substrate_notes.py",
    ],
)
def test_guardrail_allows_reads_sinks_and_body_names(tmp_path, command):
    assert inspect_bash_command(command, agent_dir=tmp_path / "agent") is None


@pytest.mark.parametrize(
    "command",
    [
        "echo x > ../runner.py",
        "echo x >../watchdog.py",
        "echo x > ../substrate/config.py",
        "cat /etc/passwd > /tmp/escape",
        "cat /etc/passwd | tee ../escape",
        "cat /etc/passwd; touch ../escape",
        "rm -rf /etc",
        "python ../runner.py",
        "kill -9 100",
        "crontab -e",
        "systemctl restart svc",
        "echo x > /dev/random",
    ],
)
def test_guardrail_still_blocks_writes_and_process_control(tmp_path, command):
    assert inspect_bash_command(command, agent_dir=tmp_path / "agent") is not None


def test_path_tools_match_paths_and_read_hook_is_unrestricted(tmp_path):
    cfg = Config(root=tmp_path)
    for name in (
        "test_runner.py",
        "test_watchdog.py",
        "substrate_notes.py",
        "runner.py",
    ):
        assert inspect_path(name, agent_dir=cfg.agent_dir) is None
        assert inspect_path(str(cfg.agent_dir / name), agent_dir=cfg.agent_dir) is None
    for name in ("../runner.py", "../watchdog.py", "../substrate/config.py"):
        assert inspect_path(name, agent_dir=cfg.agent_dir)
    hooks = SubstrateHooks(cfg, StateStore(cfg))
    assert not hooks.before_tool_call(
        SimpleNamespace(name="read"), SimpleNamespace(path="../runner.py"), None
    ).blocked


@pytest.mark.parametrize("prefix", ["", "agent/"])
def test_git_ignores_runtime_cruft(lineage_repo, prefix):
    paths = [
        prefix + p
        for p in (
            "__pycache__/x.pyc",
            ".pytest_cache/x",
            ".ruff_cache/x",
            "a",
            "kaizen-full.log",
            "_idx.txt",
            "_wrk.txt",
            "existing_works.txt",
            "index_ids2.txt",
            "work_files2.txt",
            "old-export/x",
            "journal/journal/gen-0001.log",
            "JOURNAL.md",
        )
    ]
    result = subprocess.run(
        ["git", "-C", str(lineage_repo), "check-ignore", "-z", "--stdin"],
        input="\0".join(paths) + "\0",
        text=True,
        capture_output=True,
        check=True,
    )
    assert set(result.stdout.rstrip("\0").split("\0")) == set(paths)


def test_modified_source_and_configured_generated_paths(lineage_repo):
    from substrate.progress import is_substantive

    cfg = Config(
        root=lineage_repo,
        completion_loop_threshold=1,
        completion_ignored_globs=("generated-pages/*",),
    )
    store = setup(cfg)
    assert not is_substantive("generated-pages/nested/index.html", cfg)
    assert is_substantive("handwritten.html", cfg)

    def edit_source(ctx):
        path = cfg.agent_dir / "main.py"
        path.write_text(
            path.read_text(encoding="utf-8") + "\n# new source revision\n",
            encoding="utf-8",
        )
        ctx.sink.requested = True
        ctx.sink.reason = "modified source"

    assert Runner(cfg, body_runner=edit_source).run_one_generation() == Exit.GRACEFUL
    assert store.load_status().consecutive_no_substance == 0
    assert not store.load_status().halted


def test_hard_killed_generation_resets_completion_counter(lineage_repo):
    cfg = Config(root=lineage_repo)
    store = setup(cfg)
    store.save_status(Status(consecutive_no_substance=2))
    proc = SimpleNamespace(returncode=1, poll=lambda: 1)
    Watchdog(cfg, spawn=lambda: proc)._spawn_and_supervise()
    assert store.load_status().consecutive_no_substance == 0


def test_quoted_body_path_and_nested_escape(tmp_path):
    agent = tmp_path / "body with spaces"
    assert (
        inspect_bash_command(f'touch "{agent / "test_runner.py"}"', agent_dir=agent)
        is None
    )
    assert inspect_bash_command("touch src/../../runner.py", agent_dir=agent)


def test_source_seed_index_and_build_exclusions(lineage_repo):
    from fnmatch import fnmatchcase

    tracked = (
        subprocess.check_output(
            ["git", "-C", str(lineage_repo), "ls-files", "-z"], text=True
        )
        .rstrip("\0")
        .split("\0")
    )
    bad = (
        "*.pyc",
        "*.log",
        "a",
        "_idx.txt",
        "_wrk.txt",
        "existing_works.txt",
        "index_ids*.txt",
        "work_files*.txt",
        "*-export",
        "journal",
        "runs",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        "JOURNAL.md",
    )
    for path in tracked:
        assert not any(
            fnmatchcase(part, pattern) for part in path.split("/") for pattern in bad
        ), path
    assert "agent/.gitignore" in tracked
    rules = (lineage_repo / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert ".gitignore" not in rules
    for rule in (
        "**/journal",
        "runs",
        "**/*-export",
        "**/a",
        "**/*.log",
        "**/_idx.txt",
        "**/_wrk.txt",
        "**/existing_works.txt",
        "**/index_ids*.txt",
        "**/work_files*.txt",
        "**/__pycache__",
        "**/.pytest_cache",
        "**/.ruff_cache",
        "JOURNAL.md",
    ):
        assert rule in rules
