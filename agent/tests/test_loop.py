"""Loop tests driven by a stubbed provider — no real API calls.

The stub has the exact shape of `provider.complete`: it ignores the model and
returns scripted `Completion` objects in order. That lets us assert the loop's
behavior — tool dispatch, transcript shape, validation feedback, the terminate
hint — deterministically and offline.
"""

from __future__ import annotations

from typing import Any

from spine import Agent, default_tools
from spine.hooks import BeforeToolCall, Hooks
from spine.provider import Completion, ToolCall


class StubProvider:
    """A `complete`-shaped callable that replays queued completions and records
    the messages it was asked to continue from."""

    def __init__(self, *completions: Completion) -> None:
        self._queue = list(completions)
        self.calls: list[list[dict[str, Any]]] = []

    def __call__(self, model, messages, tools=None) -> Completion:
        self.calls.append([dict(m) for m in messages])
        return self._queue.pop(0)


def make_agent(provider: StubProvider, **kwargs) -> Agent:
    return Agent(model="stub", tools=default_tools(), complete=provider, **kwargs)


def test_text_only_completion_returns_immediately():
    provider = StubProvider(Completion(content="all done"))
    agent = make_agent(provider)

    result = agent.run("say hi")

    assert result == "all done"
    assert len(provider.calls) == 1


def test_loop_runs_a_tool_then_finishes(tmp_path):
    target = tmp_path / "out.txt"
    provider = StubProvider(
        Completion(
            content=None,
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name="write",
                    arguments={"path": str(target), "content": "written by the loop"},
                )
            ],
        ),
        Completion(content="wrote the file"),
    )
    agent = make_agent(provider)

    result = agent.run("create the file")

    # The tool actually ran.
    assert target.read_text(encoding="utf-8") == "written by the loop"
    assert result == "wrote the file"

    # The transcript carries the tool result back to the model, keyed by id.
    tool_msgs = [m for m in agent.messages if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "call_1"

    # The second provider call saw the tool result in its messages.
    assert any(m["role"] == "tool" for m in provider.calls[1])


def test_validation_error_is_fed_back_not_raised():
    # `read` requires `path`; omit it. The loop must turn the ValidationError
    # into a tool message, not raise, then continue.
    provider = StubProvider(
        Completion(
            content=None,
            tool_calls=[ToolCall(id="c1", name="read", arguments={})],
        ),
        Completion(content="recovered"),
    )
    agent = make_agent(provider)

    result = agent.run("read something")

    assert result == "recovered"
    tool_msg = next(m for m in agent.messages if m["role"] == "tool")
    assert "Invalid arguments" in tool_msg["content"]


def test_unknown_tool_is_reported_to_the_model():
    provider = StubProvider(
        Completion(
            content=None,
            tool_calls=[ToolCall(id="c1", name="nonesuch", arguments={})],
        ),
        Completion(content="ok"),
    )
    agent = make_agent(provider)

    agent.run("do a thing")

    tool_msg = next(m for m in agent.messages if m["role"] == "tool")
    assert "Unknown tool" in tool_msg["content"]


def test_before_hook_can_block_a_tool_call(tmp_path):
    target = tmp_path / "guarded.txt"

    class BlockWrites(Hooks):
        def before_tool_call(self, tool, args, agent) -> BeforeToolCall:
            if tool.name == "write":
                return BeforeToolCall(blocked=True, message="writes are not allowed")
            return BeforeToolCall()

    provider = StubProvider(
        Completion(
            content=None,
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="write",
                    arguments={"path": str(target), "content": "nope"},
                )
            ],
        ),
        Completion(content="understood"),
    )
    agent = make_agent(provider, hooks=BlockWrites())

    agent.run("try to write")

    assert not target.exists()  # the block prevented execution
    tool_msg = next(m for m in agent.messages if m["role"] == "tool")
    assert "writes are not allowed" in tool_msg["content"]


def test_terminate_hint_stops_the_batch():
    class TerminateAfterBash(Hooks):
        def after_tool_call(self, tool, args, result, agent):
            result.terminate = True
            return result

    provider = StubProvider(
        Completion(
            content="stopping now",
            tool_calls=[
                ToolCall(id="c1", name="bash", arguments={"command": "echo hi"})
            ],
        ),
        # This second completion must NOT be consumed if terminate works.
        Completion(content="should not be reached"),
    )
    agent = make_agent(provider, hooks=TerminateAfterBash())

    result = agent.run("run and stop")

    assert result == "stopping now"
    assert len(provider.calls) == 1  # loop stopped after the first batch
