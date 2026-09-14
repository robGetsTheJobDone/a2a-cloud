"""Runtime scope negotiation: ``RunContext.request_scope`` end-to-end."""
from __future__ import annotations

import asyncio

import pytest

from a2a_pack import A2AAgent, NoAuth, RunContext, skill
from a2a_pack.context import (
    LocalRunContext,
    ScopeDenied,
    ScopeExpansionNotAllowed,
    _ScopeRegistry,
)
from a2a_pack.grants import mint_grant
from a2a_pack.workspace import (
    LocalWorkspaceClient,
    WorkspaceAccess,
    WorkspaceMode,
)


# ---------------------------------------------------------------------------
# Two skills: one opted in, one not. Lets us test the gate cleanly.
# ---------------------------------------------------------------------------


class _Probe(A2AAgent[None, NoAuth]):  # type: ignore[type-var]
    name = "probe"
    description = "scope-negotiation test rig"
    auth_model = NoAuth

    @skill(description="Tries to expand scope", allow_scope_expansion=True)
    async def expand(self, ctx: RunContext[NoAuth]) -> dict:
        new_grant = await ctx.request_scope(
            reason="needs more files",
            read=["reference/**"],
            ttl_seconds=120,
            mode="read_only",
            timeout=90,
            approval_timeout=45,
        )
        return {"new_grant_id": new_grant.grant_id}

    @skill(description="Tries to add write prefixes", allow_scope_expansion=True)
    async def expand_write(self, ctx: RunContext[NoAuth]) -> dict:
        new_grant = await ctx.request_scope(
            reason="needs result folders",
            write_prefixes=("reports/", "charts/"),
            ttl_seconds=120,
            mode="read_write_overlay",
            timeout=90,
        )
        return {"new_grant_id": new_grant.grant_id}

    @skill(description="Tries to expand without opting in")
    async def expand_blocked(self, ctx: RunContext[NoAuth]) -> dict:
        await ctx.request_scope(
            reason="should be blocked",
            read=["**"],
            ttl_seconds=60,
        )
        return {"unreachable": True}


def _workspace(allow_patterns: tuple[str, ...] = ("data/**",)) -> LocalWorkspaceClient:
    access = WorkspaceAccess.dynamic(
        allowed_modes=(WorkspaceMode.READ_ONLY, WorkspaceMode.READ_WRITE_OVERLAY),
    )
    ws = LocalWorkspaceClient(
        files={}, access=access, bucket="user-1-files", issuer="self"
    )
    _, token = mint_grant(
        issuer="cp",
        audience="probe",
        bucket="user-1-files",
        mode=WorkspaceMode.READ_ONLY,
        allow_patterns=allow_patterns,
        ttl_seconds=300,
    )
    from a2a_pack.grants import verify_grant
    ws.install_grant(verify_grant(token))
    return ws


@pytest.mark.asyncio
async def test_skill_without_opt_in_is_blocked() -> None:
    """A skill that doesn't declare allow_scope_expansion can't call request_scope."""
    agent = _Probe()
    ctx: LocalRunContext[NoAuth] = LocalRunContext(
        auth=NoAuth(), workspace=_workspace(),
    )
    with pytest.raises(ScopeExpansionNotAllowed):
        await agent.invoke("expand_blocked", ctx)


@pytest.mark.asyncio
async def test_request_scope_resolves_with_signed_grant() -> None:
    """When the platform delivers a fresh signed grant, the future resolves."""
    agent = _Probe()
    ws = _workspace()
    ctx: LocalRunContext[NoAuth] = LocalRunContext(
        auth=NoAuth(), workspace=ws,
    )

    # Run the skill in the background; capture the request_id from the
    # emitted scope_request event, then deliver a superseding grant.
    async def deliver_after_event() -> None:
        # Wait for emit_event to fire.
        for _ in range(100):
            scope_events = [e for e in ctx.events if e.kind == "scope_request"]
            if scope_events:
                break
            await asyncio.sleep(0.01)
        payload = scope_events[0].payload
        request_id = payload["request_id"]
        assert payload["timeout_seconds"] == 90
        assert payload["approval_timeout_seconds"] == 45
        assert payload["write_prefixes"] == []
        _, token = mint_grant(
            issuer="cp",
            audience="probe",
            bucket="user-1-files",
            mode=WorkspaceMode.READ_ONLY,
            allow_patterns=("data/**", "reference/**"),
            ttl_seconds=120,
        )
        assert RunContext.resolve_scope_grant(request_id, token) is True

    deliver_task = asyncio.create_task(deliver_after_event())
    result = await agent.invoke("expand", ctx)
    await deliver_task

    assert "new_grant_id" in result
    # workspace was updated to reflect the superseding grant
    assert "reference/**" in ws.allow_patterns
    assert "data/**" in ws.allow_patterns


@pytest.mark.asyncio
async def test_request_scope_emits_and_installs_multiple_write_prefixes() -> None:
    agent = _Probe()
    ws = _workspace()
    ctx: LocalRunContext[NoAuth] = LocalRunContext(auth=NoAuth(), workspace=ws)

    async def deliver_after_event() -> None:
        for _ in range(100):
            scope_events = [e for e in ctx.events if e.kind == "scope_request"]
            if scope_events:
                break
            await asyncio.sleep(0.01)
        payload = scope_events[0].payload
        assert payload["write_prefixes"] == ["reports/", "charts/"]
        _, token = mint_grant(
            issuer="cp",
            audience="probe",
            bucket="user-1-files",
            mode=WorkspaceMode.READ_WRITE_OVERLAY,
            allow_patterns=("data/**",),
            outputs_prefix="reports/",
            write_prefixes=("charts/",),
            ttl_seconds=120,
        )
        assert RunContext.resolve_scope_grant(payload["request_id"], token) is True

    deliver_task = asyncio.create_task(deliver_after_event())
    result = await agent.invoke("expand_write", ctx)
    await deliver_task

    assert "new_grant_id" in result
    assert ws.write_prefixes == ("reports/", "charts/")
    assert ws.is_writable_output("charts/chart.png")


@pytest.mark.asyncio
async def test_ensure_helpers_noop_when_current_grant_covers_request() -> None:
    ws = _workspace(allow_patterns=("data/**",))
    _, token = mint_grant(
        issuer="cp",
        audience="probe",
        bucket="user-1-files",
        mode=WorkspaceMode.READ_WRITE_OVERLAY,
        allow_patterns=("data/**",),
        outputs_prefix="outputs/",
        ttl_seconds=300,
    )
    from a2a_pack.grants import verify_grant
    ws.install_grant(verify_grant(token))
    ctx: LocalRunContext[NoAuth] = LocalRunContext(auth=NoAuth(), workspace=ws)
    ctx._scope_expansion_allowed = True

    assert await ctx.ensure_read(reason="already covered", patterns=("data/input.csv",)) is None
    assert await ctx.ensure_write(reason="already writable", prefix="outputs/reports") is None
    assert [e for e in ctx.events if e.kind == "scope_request"] == []


@pytest.mark.asyncio
async def test_request_scope_propagates_denial() -> None:
    agent = _Probe()
    ctx: LocalRunContext[NoAuth] = LocalRunContext(
        auth=NoAuth(), workspace=_workspace(),
    )

    async def deny_after_event() -> None:
        for _ in range(100):
            scope_events = [e for e in ctx.events if e.kind == "scope_request"]
            if scope_events:
                break
            await asyncio.sleep(0.01)
        request_id = scope_events[0].payload["request_id"]
        assert RunContext.deny_scope(request_id, "policy: cross-bucket") is True

    deny_task = asyncio.create_task(deny_after_event())
    with pytest.raises(ScopeDenied, match="cross-bucket"):
        await agent.invoke("expand", ctx)
    await deny_task


@pytest.mark.asyncio
async def test_install_grant_on_workspace_replaces_policy() -> None:
    ws = _workspace(allow_patterns=("data/**",))
    assert ws.allow_patterns == ("data/**",)
    _, token = mint_grant(
        issuer="cp",
        audience="probe",
        bucket="user-1-files",
        mode=WorkspaceMode.READ_WRITE_OVERLAY,
        allow_patterns=("data/**", "reference/**", "config/*.yaml"),
        outputs_prefix="results/",
        ttl_seconds=300,
    )
    from a2a_pack.grants import verify_grant
    ws.install_grant(verify_grant(token))

    assert ws.allow_patterns == ("data/**", "reference/**", "config/*.yaml")
    assert ws.outputs_prefix == "results/"
    assert ws.write_prefixes == ("results/",)
    assert ws.current_mode is WorkspaceMode.READ_WRITE_OVERLAY


def test_resolve_scope_with_invalid_grant_token_returns_denial() -> None:
    """An invalid signature returned by the platform turns into a ScopeDenied."""
    # Stand up a synthetic future and resolve with garbage.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        async def go() -> str | None:
            fut: asyncio.Future = loop.create_future()
            _ScopeRegistry._waiters["sr_test"] = fut
            try:
                ok = RunContext.resolve_scope_grant("sr_test", "not.a.grant")
                assert ok is True
                outcome = await asyncio.wait_for(fut, timeout=0.5)
                return outcome if isinstance(outcome, str) else None
            finally:
                _ScopeRegistry._waiters.pop("sr_test", None)

        msg = loop.run_until_complete(go())
        assert msg is not None
        assert "invalid grant" in msg
    finally:
        loop.close()
