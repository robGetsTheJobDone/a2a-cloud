"""Approval-mode hand-off: dashboard-style flow.

With ``approval_mode=True`` the orchestrator emits an ``approval_required``
event and blocks before the handoff actually fires. The test runs two
asyncio tasks in parallel: one consumes the SSE stream and looks for the
approval prompt; the other (on first sight) POSTs the user's
``approve`` decision back to ``/v1/me/chat/approvals/{id}``.

If both halves work, the chat proceeds past the handoff and produces a
``handoff_complete`` event. If the click never lands, the handoff
auto-denies after ~2 min — we'd see ``handoff_denied``.
"""
from __future__ import annotations

import asyncio
import secrets

import pytest

from .conftest import ApiClient


@pytest.mark.slow
async def test_handoff_unlocks_after_user_approves(client: ApiClient) -> None:
    suffix = secrets.token_hex(4)
    text = f"approval-{suffix}"

    saw_approval_required = asyncio.Event()
    approved = asyncio.Event()
    pending_id: dict[str, str] = {}
    events: list[dict] = []

    async def consume_stream() -> None:
        async for ev in client.stream_sse(
            "/v1/me/chat",
            body={
                "messages": [{
                    "role": "user",
                    "content": (
                        f"Use test-helper.echo with text {text!r}."
                    ),
                }],
                "stream": True,
                "approval_mode": True,
            },
            timeout=420.0,
        ):
            events.append(ev)
            if ev.get("type") == "approval_required":
                pending_id["id"] = ev["approval_id"]
                saw_approval_required.set()

    async def click_approve() -> None:
        await asyncio.wait_for(saw_approval_required.wait(), timeout=120.0)
        approval_id = pending_id["id"]
        r = await client.post_json(
            f"/v1/me/chat/approvals/{approval_id}",
            body={"decision": "approve"},
        )
        assert r.status_code == 200, r.text
        approved.set()

    await asyncio.gather(consume_stream(), click_approve())

    assert saw_approval_required.is_set()
    assert approved.is_set()

    # Handoff fired AFTER the approval, not before.
    handoffs = [
        e for e in events
        if e.get("type") == "agent_handoff" and e.get("to") == "test-helper"
    ]
    assert handoffs, "approval succeeded but no handoff event followed"
    completes = [e for e in events if e.get("type") == "handoff_complete"]
    assert completes and completes[-1]["ok"], (
        f"handoff did not complete cleanly: {completes}"
    )


@pytest.mark.slow
async def test_handoff_denied_when_user_rejects(client: ApiClient) -> None:
    suffix = secrets.token_hex(4)
    text = f"deny-{suffix}"

    saw_approval_required = asyncio.Event()
    pending_id: dict[str, str] = {}
    events: list[dict] = []

    async def consume_stream() -> None:
        async for ev in client.stream_sse(
            "/v1/me/chat",
            body={
                "messages": [{
                    "role": "user",
                    "content": (
                        f"Use test-helper.echo with text {text!r}."
                    ),
                }],
                "stream": True,
                "approval_mode": True,
            },
            timeout=240.0,
        ):
            events.append(ev)
            if ev.get("type") == "approval_required":
                pending_id["id"] = ev["approval_id"]
                saw_approval_required.set()

    async def click_deny() -> None:
        await asyncio.wait_for(saw_approval_required.wait(), timeout=120.0)
        r = await client.post_json(
            f"/v1/me/chat/approvals/{pending_id['id']}",
            body={"decision": "deny"},
        )
        assert r.status_code == 200

    await asyncio.gather(consume_stream(), click_deny())

    # Either we see a handoff_denied event or the call_agent tool_result
    # reports ok=false with "user denied handoff" — both are valid surface
    # forms of the same outcome.
    denied = any(e.get("type") == "handoff_denied" for e in events)
    failed_calls = [
        e for e in events
        if e.get("type") == "tool_result"
        and e.get("tool") == "call_agent"
        and not e.get("ok")
    ]
    assert denied or failed_calls, (
        "deny click sent but no handoff_denied / failed call_agent event"
    )

    # Crucially: no handoff_complete with ok=true for test-helper — the
    # specific cross-agent invocation must've been rejected.
    ok_handoffs = [
        e for e in events
        if e.get("type") == "handoff_complete" and e.get("ok") is True
    ]
    assert not ok_handoffs, (
        f"deny was acknowledged but a hand-off still completed ok=true: "
        f"{ok_handoffs}"
    )
