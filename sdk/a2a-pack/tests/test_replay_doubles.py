"""Re-execution doubles: a captured ReplaySession must reproduce the original
output, and mutating the agent away from the recording must raise
:class:`ReplayDivergence`.
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel

from a2a_pack import (
    A2AAgent,
    LocalRunContext,
    NoAuth,
    ReplayDivergence,
    ReplayLLM,
    ReplaySession,
    ReplayToolCaller,
    ReplayWorkspaceClient,
    RunContext,
    replay_session,
    skill,
    verify_replay_session,
)


class _Cfg(BaseModel):
    pass


class _DeterministicAgent(A2AAgent[_Cfg, NoAuth]):
    """Skill that only consumes ``ctx.random()`` — fully deterministic given
    the recorded random_seed."""

    name = "deterministic-agent"
    description = "Returns reproducible draws"
    config_model = _Cfg
    auth_model = NoAuth

    @skill(description="draw n integers")
    async def draws(self, ctx: RunContext[NoAuth], n: int = 4) -> dict:
        rng = ctx.random()
        return {"values": [rng.randint(0, 1000) for _ in range(n)]}


class _MutantAgent(A2AAgent[_Cfg, NoAuth]):
    """Same skill name, different behavior — used to detect divergence by
    proxying the call through divergent ctx.workspace usage."""

    name = "deterministic-agent"
    description = "Tries to write a file the recording didn't"
    config_model = _Cfg
    auth_model = NoAuth

    @skill(description="draw n integers")
    async def draws(self, ctx: RunContext[NoAuth], n: int = 4) -> dict:
        rng = ctx.random()
        ws = getattr(ctx, "workspace", None)
        if ws is not None:
            ws.write("not/in/recording.txt", b"hello")
        return {"values": [rng.randint(0, 1000) for _ in range(n)]}


async def _capture_session(agent: A2AAgent) -> tuple[dict, ReplaySession]:
    ctx: LocalRunContext[NoAuth] = LocalRunContext(
        auth=NoAuth(),
        task_id="t-capture",
        caller="user:1",
        random_seed="seed-doubles-test",
    )
    result = await agent.invoke("draws", ctx, n=5)
    replay_evs = [ev for ev in ctx.events if ev.kind == "replay_sealed"]
    assert replay_evs, "live run must seal a replay session"
    session = verify_replay_session(replay_evs[-1].payload["token"])
    return result, session


@pytest.mark.asyncio
async def test_replay_session_reproduces_deterministic_output() -> None:
    agent = _DeterministicAgent()
    live, session = await _capture_session(agent)
    replayed = await replay_session(agent, session)
    assert replayed == live


@pytest.mark.asyncio
async def test_replay_session_raises_on_divergent_workspace_write() -> None:
    """A mutant agent that writes a file the recording didn't see must
    surface as :class:`ReplayDivergence`."""
    captured_agent = _DeterministicAgent()
    _, session = await _capture_session(captured_agent)

    mutant = _MutantAgent()
    with pytest.raises(Exception) as exc_info:
        await replay_session(mutant, session)
    # SkillInvocationError wraps the underlying divergence — unwrap one
    # level if necessary.
    err = exc_info.value
    if not isinstance(err, ReplayDivergence):
        err = err.__cause__ or err
    assert isinstance(err, ReplayDivergence)


def test_replay_doubles_consume_in_recorded_order() -> None:
    """Hand-built session: doubles must yield payloads in order and raise
    ``ReplayDivergence`` when exhausted."""
    from a2a_pack.replay import EventRecorder

    rec = EventRecorder(agent_name="a", skill_name="s")
    rec.record("workspace_read", {"path": "data/a.txt", "data": "alpha"})
    rec.record("workspace_read", {"path": "data/b.txt", "data": "bravo"})
    rec.record("workspace_write", {"path": "out/result.txt"})
    session = rec.build_session(receipt_id="r1")

    ws = ReplayWorkspaceClient(session)
    assert ws.read("data/a.txt") == b"alpha"
    assert ws.read("data/b.txt") == b"bravo"
    with pytest.raises(ReplayDivergence):
        ws.read("data/c.txt")  # no more workspace_read events

    ws2 = ReplayWorkspaceClient(session)
    with pytest.raises(ReplayDivergence):
        ws2.write("different/path.txt", b"x")


def test_replay_llm_double_round_trips() -> None:
    from a2a_pack.replay import EventRecorder

    rec = EventRecorder(agent_name="a", skill_name="s")
    rec.record("llm_call", {"model": "m-1", "prompt": "hello"})
    rec.record("llm_response", {"response": "world"})
    session = rec.build_session()

    llm = ReplayLLM(session)
    assert llm.complete(model="m-1", prompt="hello") == "world"
    with pytest.raises(ReplayDivergence):
        llm.complete(model="m-1", prompt="hello")  # exhausted


def test_replay_llm_double_detects_signature_mismatch() -> None:
    from a2a_pack.replay import EventRecorder

    rec = EventRecorder(agent_name="a", skill_name="s")
    rec.record("llm_call", {"model": "m-1", "prompt": "hello"})
    rec.record("llm_response", {"response": "world"})
    session = rec.build_session()

    llm = ReplayLLM(session)
    with pytest.raises(ReplayDivergence):
        llm.complete(model="m-different", prompt="hello")


def test_replay_tool_caller_round_trip_and_mismatch() -> None:
    from a2a_pack.replay import EventRecorder

    rec = EventRecorder(agent_name="a", skill_name="s")
    rec.record("tool_call", {"name": "search", "q": "foo"})
    rec.record("tool_response", {"response": [1, 2, 3]})
    session = rec.build_session()

    tools = ReplayToolCaller(session)
    assert tools.call("search", q="foo") == [1, 2, 3]

    rec2 = EventRecorder(agent_name="a", skill_name="s")
    rec2.record("tool_call", {"name": "search", "q": "foo"})
    rec2.record("tool_response", {"response": "ok"})
    session2 = rec2.build_session()
    tools2 = ReplayToolCaller(session2)
    with pytest.raises(ReplayDivergence):
        tools2.call("different-tool", q="foo")
