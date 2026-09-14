"""Receipts + replay sessions are emitted by ``A2AAgent.invoke()``.

Every successful and failed invocation should put a ``receipt_sealed`` +
``replay_sealed`` event on the context. The tokens must verify with Ed25519
runtime keys.
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel

from a2a_pack import (
    A2AAgent,
    AgentEvent,
    LocalRunContext,
    NoAuth,
    RunContext,
    SkillInvocationError,
    skill,
    verify_receipt,
    verify_replay_session,
)


class _Config(BaseModel):
    pass


class _ReceiptAgent(A2AAgent[_Config, NoAuth]):
    name = "receipt-agent"
    description = "Issues signed receipts + replay sessions"
    config_model = _Config
    auth_model = NoAuth
    version = "1.2.3"

    @skill(description="ok skill")
    async def ok_skill(self, ctx: RunContext[NoAuth], x: int) -> dict:
        return {"x_doubled": x * 2}

    @skill(description="boom skill")
    async def boom(self, ctx: RunContext[NoAuth]) -> str:
        raise RuntimeError("nope")


def _filter_kind(events: list[AgentEvent], kind: str) -> list[AgentEvent]:
    return [ev for ev in events if ev.kind == kind]


@pytest.mark.asyncio
async def test_invoke_emits_receipt_and_replay_for_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _ReceiptAgent()
    ctx: LocalRunContext[NoAuth] = LocalRunContext(
        auth=NoAuth(),
        task_id="t-1",
        caller="user:42",
        grant_ids=("g_abc",),
    )

    result = await agent.invoke("ok_skill", ctx, x=21)
    assert result == {"x_doubled": 42}

    receipt_events = _filter_kind(ctx.events, "receipt_sealed")
    replay_events = _filter_kind(ctx.events, "replay_sealed")
    assert len(receipt_events) == 1
    assert len(replay_events) == 1

    receipt_token = receipt_events[0].payload["token"]
    receipt = verify_receipt(receipt_token)
    assert receipt.receipt_id == receipt_events[0].payload["receipt_id"]
    assert receipt.agent_name == "receipt-agent"
    assert receipt.agent_version == "1.2.3"
    assert receipt.skill_name == "ok_skill"
    assert receipt.caller == "user:42"
    assert receipt.task_id == "t-1"
    assert receipt.grant_ids == ("g_abc",)
    assert receipt.status == "ok"

    session_token = replay_events[0].payload["token"]
    session = verify_replay_session(session_token)
    assert session.session_id == replay_events[0].payload["session_id"]
    assert session.receipt_id == receipt.receipt_id
    assert session.agent_name == "receipt-agent"
    assert session.skill_name == "ok_skill"
    assert session.caller == "user:42"
    assert session.task_id == "t-1"
    kinds = [ev.kind for ev in session.events]
    assert "skill_start" in kinds
    assert "skill_end" in kinds


@pytest.mark.asyncio
async def test_invoke_seals_error_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _ReceiptAgent()
    ctx: LocalRunContext[NoAuth] = LocalRunContext(
        auth=NoAuth(),
        caller="user:9",
        grant_ids=("g_zzz",),
    )
    with pytest.raises(SkillInvocationError):
        await agent.invoke("boom", ctx)

    receipt_events = _filter_kind(ctx.events, "receipt_sealed")
    assert len(receipt_events) == 1
    receipt = verify_receipt(receipt_events[0].payload["token"])
    assert receipt.status == "error"
    assert receipt.error_type == "RuntimeError"
    assert receipt.caller == "user:9"
    assert receipt.grant_ids == ("g_zzz",)

    replay_events = _filter_kind(ctx.events, "replay_sealed")
    assert len(replay_events) == 1
    session = verify_replay_session(replay_events[0].payload["token"])
    kinds = [ev.kind for ev in session.events]
    assert "skill_start" in kinds
    assert "error" in kinds
    assert "skill_end" not in kinds


@pytest.mark.asyncio
async def test_invoke_requires_ed25519_runtime_keys_for_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("A2A_RECEIPT_SIGNING_KEY", raising=False)
    monkeypatch.delenv("A2A_RECEIPT_VERIFYING_KEY", raising=False)
    monkeypatch.delenv("A2A_REPLAY_SIGNING_KEY", raising=False)
    monkeypatch.delenv("A2A_REPLAY_VERIFYING_KEY", raising=False)

    agent = _ReceiptAgent()
    ctx: LocalRunContext[NoAuth] = LocalRunContext(auth=NoAuth())
    result = await agent.invoke("ok_skill", ctx, x=2)
    assert result == {"x_doubled": 4}

    assert _filter_kind(ctx.events, "receipt_sealed") == []
    assert _filter_kind(ctx.events, "replay_sealed") == []
    assert _filter_kind(ctx.events, "receipt_error")


@pytest.mark.asyncio
async def test_gateway_signing_mode_leaves_evidence_to_trusted_ingress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("A2A_EVIDENCE_SIGNING_MODE", "gateway")

    agent = _ReceiptAgent()
    ctx: LocalRunContext[NoAuth] = LocalRunContext(auth=NoAuth())
    result = await agent.invoke("ok_skill", ctx, x=3)

    assert result == {"x_doubled": 6}
    assert _filter_kind(ctx.events, "receipt_sealed") == []
    assert _filter_kind(ctx.events, "replay_sealed") == []
    assert _filter_kind(ctx.events, "receipt_error") == []
