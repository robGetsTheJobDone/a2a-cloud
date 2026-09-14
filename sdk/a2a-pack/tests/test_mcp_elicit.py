"""End-to-end test for MCP elicitation over real HTTP.

A skill that calls ``ctx.collect()`` mid-execution should round-trip an
``elicitation/create`` request to the MCP client and resume with the
typed response. Driven against a real uvicorn server bound to a free
local port so the SSE response and the elicit-response POST land on
separate HTTP connections — matching what an actual MCP client does.
"""
from __future__ import annotations

import asyncio
import json
import socket
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from pydantic import BaseModel

from a2a_pack import A2AAgent, NoAuth, RunContext, skill
from a2a_pack.mcp.http import build_http_app


# --- agents under test ----------------------------------------------------- #


class _Confirmation(BaseModel):
    confirmed: bool
    reason: str = ""


class _Asker(A2AAgent):
    name = "asker-mcp"
    version = "0.1.0"
    description = "Asks the caller to confirm an action."

    @skill(description="Ask the user to confirm.")
    async def confirm(self, ctx: RunContext[NoAuth]) -> dict:
        c = await ctx.collect(
            _Confirmation,
            title="Confirm action",
            reason="checking before proceeding",
            timeout=10.0,
        )
        return {"confirmed": c.confirmed, "reason": c.reason}


class _ShortAsker(A2AAgent):
    name = "short-asker-mcp"

    @skill(description="Asks with a tight timeout.")
    async def confirm(self, ctx: RunContext[NoAuth]) -> dict:
        c = await ctx.collect(_Confirmation, title="?", timeout=0.5)
        return {"confirmed": c.confirmed}


class _Greeter(A2AAgent):
    name = "greeter-mcp-elicit"

    @skill(description="Greet someone.")
    async def greet(self, ctx: RunContext[NoAuth], who: str) -> str:
        return f"hi {who}"


class _HangingAgent(A2AAgent):
    name = "hanging-mcp"

    def __init__(self) -> None:
        super().__init__()
        self.cancelled = asyncio.Event()

    @skill(description="Emit progress and keep running.")
    async def hang(self, ctx: RunContext[NoAuth]) -> dict:
        try:
            await ctx.emit_progress("started")
            await asyncio.Future()
            return {"ok": True}
        finally:
            self.cancelled.set()


# --- uvicorn fixture ------------------------------------------------------- #


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@asynccontextmanager
async def _live_server(agent: A2AAgent) -> AsyncIterator[str]:
    """Run uvicorn in the test's own event loop on a free local port.

    Avoids threaded uvicorn: that pattern leaks daemon threads + bound
    sockets across tests because ``uvicorn.Server.should_exit`` only
    fires at next loop iteration, which a thread running ``asyncio.run``
    independently may never reach during pytest teardown.
    """
    port = _free_port()
    app = build_http_app(agent)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    # Wait for the socket to accept.
    deadline = asyncio.get_running_loop().time() + 5.0
    while asyncio.get_running_loop().time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            await asyncio.sleep(0.02)
    else:
        server.should_exit = True
        await serve_task
        raise RuntimeError("uvicorn did not start in time")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(serve_task, timeout=5.0)
        except asyncio.TimeoutError:
            serve_task.cancel()


# --- helpers --------------------------------------------------------------- #


async def _iter_sse(
    response: httpx.Response,
) -> AsyncIterator[dict]:
    """Yield each ``data: …\\n\\n`` frame from an SSE response."""
    buf = b""
    async for chunk in response.aiter_bytes():
        buf += chunk
        while b"\n\n" in buf:
            frame, _, buf = buf.partition(b"\n\n")
            for ln in frame.split(b"\n"):
                if ln.startswith(b"data:"):
                    yield json.loads(ln[5:].lstrip())
                    break


# --- integration tests ----------------------------------------------------- #


@pytest.mark.asyncio
async def test_mcp_sse_disconnect_cancels_running_tool() -> None:
    agent = _HangingAgent()
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "hang", "arguments": {}},
    }

    async with _live_server(agent) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            async with client.stream(
                "POST",
                "/mcp",
                json=body,
                headers={"Accept": "text/event-stream"},
            ) as resp:
                assert resp.headers["content-type"].startswith("text/event-stream")
                async for event in _iter_sse(resp):
                    if event.get("method") == "notifications/progress":
                        break

        await asyncio.wait_for(agent.cancelled.wait(), timeout=5.0)


@pytest.mark.asyncio
async def test_tools_call_elicits_and_resumes_over_real_http() -> None:
    """Open tools/call SSE, expect elicitation/create, deliver response on
    a parallel POST, expect the final tool result on the SSE stream."""
    async with _live_server(_Asker()) as base_url, \
            httpx.AsyncClient(base_url=base_url, timeout=15.0) as client:
        async with client.stream(
            "POST",
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "confirm", "arguments": {}},
            },
            headers={"Accept": "text/event-stream"},
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            session_id = resp.headers.get("Mcp-Session-Id")
            assert session_id

            events: list[dict] = []
            deliver_task: asyncio.Task | None = None
            async for msg in _iter_sse(resp):
                events.append(msg)
                if msg.get("method") == "elicitation/create":
                    assert msg["params"]["requestedSchema"]["type"] == "object"
                    elicit_id = msg["id"]

                    async def _deliver(eid: str) -> httpx.Response:
                        return await client.post(
                            "/mcp",
                            json={
                                "jsonrpc": "2.0",
                                "id": eid,
                                "result": {
                                    "action": "accept",
                                    "content": {
                                        "confirmed": True,
                                        "reason": "ok",
                                    },
                                },
                            },
                            headers={"Mcp-Session-Id": session_id},
                        )

                    deliver_task = asyncio.create_task(_deliver(elicit_id))
                if msg.get("id") == 1 and "result" in msg:
                    break

            assert deliver_task is not None
            deliver_resp = await deliver_task
            assert deliver_resp.status_code == 202

    assert len(events) == 2
    final = events[-1]
    assert final["id"] == 1
    assert final["result"]["structuredContent"] == {
        "confirmed": True,
        "reason": "ok",
    }


@pytest.mark.asyncio
async def test_elicit_decline_lets_skill_time_out_over_real_http() -> None:
    """If the client returns ``action="decline"`` the skill's own timeout
    fires and the tool surfaces an isError result on the SSE stream."""
    async with _live_server(_ShortAsker()) as base_url, \
            httpx.AsyncClient(base_url=base_url, timeout=15.0) as client:
        async with client.stream(
            "POST",
            "/mcp",
            json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "confirm", "arguments": {}},
            },
            headers={"Accept": "text/event-stream"},
        ) as resp:
            assert resp.status_code == 200
            session_id = resp.headers["Mcp-Session-Id"]
            events: list[dict] = []
            async for msg in _iter_sse(resp):
                events.append(msg)
                if msg.get("method") == "elicitation/create":
                    r = await client.post(
                        "/mcp",
                        json={
                            "jsonrpc": "2.0",
                            "id": msg["id"],
                            "result": {"action": "decline"},
                        },
                        headers={"Mcp-Session-Id": session_id},
                    )
                    assert r.status_code == 202
                if msg.get("id") == 1 and "result" in msg:
                    break
    final = events[-1]
    assert final["result"]["isError"] is True


# --- single-process sanity tests (TestClient) ------------------------------ #


def test_tools_call_without_sse_accept_uses_plain_json() -> None:
    """Backwards-compat: clients that don't accept SSE keep the original
    JSON-response behavior. Skills that don't elicit work unchanged."""
    client = TestClient(build_http_app(_Greeter()))
    r = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "greet", "arguments": {"who": "world"}},
        },
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    data = r.json()
    assert data["result"]["structuredContent"] == {"result": "hi world"}


def test_initialize_advertises_elicitation_capability() -> None:
    client = TestClient(build_http_app(_Greeter()))
    r = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert r.status_code == 200
    caps = r.json()["result"]["capabilities"]
    assert "elicitation" in caps


def test_elicit_response_with_unknown_session_returns_404() -> None:
    client = TestClient(build_http_app(_Greeter()))
    r = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": "srv-bogus", "result": {"action": "decline"}},
        headers={"Mcp-Session-Id": "does-not-exist"},
    )
    assert r.status_code == 404


def test_elicit_response_without_session_id_returns_404() -> None:
    client = TestClient(build_http_app(_Greeter()))
    r = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": "srv-bogus", "result": {"action": "decline"}},
    )
    assert r.status_code == 404
