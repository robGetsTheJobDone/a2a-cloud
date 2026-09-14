"""End-to-end tests for the agent-to-agent + grant handoff seam."""
from __future__ import annotations

import time
import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from a2a_pack import (
    A2AAgent,
    CompositionBudget,
    CompositionLimitExceeded,
    ConsumerSetup,
    ConsumerSetupField,
    DiscoveredAgent,
    FileType,
    Grant,
    GrantDelegationDenied,
    GrantInvalid,
    InMemoryA2AClient,
    InMemoryDiscovery,
    LocalRunContext,
    LocalWorkspaceClient,
    NoAuth,
    RunContext,
    WorkspaceAccess,
    WorkspaceMode,
    mint_grant,
    subagent_tools,
    skill,
    verify_grant,
)


# ---------------------------------------------------------------------------
# grant tokens
# ---------------------------------------------------------------------------


def test_mint_and_verify_round_trip():
    grant, token = mint_grant(
        issuer="main", audience="graph", bucket="user-42-files"
    )
    out = verify_grant(token)
    assert out.grant_id == grant.grant_id
    assert out.bucket == "user-42-files"
    assert out.audience == "graph"
    assert out.expires_at > int(time.time())


def test_tampered_grant_rejected():
    _, token = mint_grant(issuer="main", audience="graph", bucket="b")
    payload, sig = token.rsplit(".", 1)
    forged = payload + "x." + sig
    with pytest.raises(GrantInvalid):
        verify_grant(forged)


def test_expired_grant_rejected():
    _, token = mint_grant(
        issuer="main",
        audience="graph",
        bucket="b",
        ttl_seconds=-1,  # already expired
    )
    with pytest.raises(GrantInvalid, match="expired"):
        verify_grant(token)


def test_signature_mismatch_rejected(monkeypatch):
    _, token = mint_grant(issuer="main", audience="graph", bucket="b")
    wrong_key = Ed25519PrivateKey.generate().public_key()
    wrong_public = wrong_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    monkeypatch.setenv(
        "A2A_GRANT_VERIFYING_KEY",
        base64.b64encode(wrong_public).decode("ascii"),
    )
    with pytest.raises(GrantInvalid, match="signature"):
        verify_grant(token)


def test_ed25519_grant_verification_with_configured_keys(monkeypatch):
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    private_raw = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    monkeypatch.setenv("A2A_GRANT_SIGNING_KEY", base64.b64encode(private_raw).decode("ascii"))
    monkeypatch.setenv("A2A_GRANT_VERIFYING_KEY", base64.b64encode(public_raw).decode("ascii"))

    grant, token = mint_grant(
        issuer="main", audience="graph", bucket="user-42-files"
    )
    parsed = verify_grant(token)

    assert parsed.grant_id == grant.grant_id
    assert parsed.bucket == "user-42-files"


# ---------------------------------------------------------------------------
# workspace.delegate() mints a grant; receiving side verifies it
# ---------------------------------------------------------------------------


async def test_workspace_delegate_mints_valid_grant():
    files = {"data/sales.xlsx": b"...", "secrets/.env": b"DO_NOT"}
    ws = LocalWorkspaceClient(
        files=files,
        access=WorkspaceAccess.dynamic(
            max_files=5,
            allowed_modes=(WorkspaceMode.READ_ONLY,),
            deny_patterns=("secrets/**",),
        ),
        bucket="user-42-files",
        issuer="main-agent",
    )
    token = await ws.delegate(
        audience="graph-agent",
        allow_patterns=("*.xlsx",),
        deny_patterns=("secrets/**",),
        outputs_prefix="charts/",
        ttl_seconds=300,
    )
    grant = verify_grant(token)
    assert grant.bucket == "user-42-files"
    assert grant.audience == "graph-agent"
    assert "*.xlsx" in grant.allow_patterns
    assert "secrets/**" in grant.deny_patterns
    assert grant.outputs_prefix == "charts/"
    assert grant.write_prefixes == ("charts/",)
    assert grant.mode is WorkspaceMode.READ_ONLY


async def test_workspace_delegate_from_installed_grant_cannot_widen_scope():
    files = {"data/sales.csv": b"...", "secrets/.env": b"DO_NOT"}
    ws = LocalWorkspaceClient(
        files=files,
        access=WorkspaceAccess.dynamic(
            max_files=5,
            allowed_modes=(WorkspaceMode.READ_ONLY, WorkspaceMode.READ_WRITE_OVERLAY),
            deny_patterns=("secrets/**",),
        ),
        bucket="user-42-files",
        issuer="meta-agent",
    )
    parent, _ = mint_grant(
        issuer="user-42",
        audience="meta-agent",
        bucket="user-42-files",
        mode=WorkspaceMode.READ_WRITE_OVERLAY,
        allow_patterns=("data/**",),
        deny_patterns=("secrets/**",),
        outputs_prefix="reports/",
        delegation_depth=1,
        max_delegation_depth=3,
        ttl_seconds=600,
    )
    ws.install_grant(parent)

    token = await ws.delegate(
        audience="chart-agent",
        allow_patterns=("data/*.csv",),
        outputs_prefix="reports/charts/",
        ttl_seconds=900,
    )
    child = verify_grant(token)

    assert child.parent_grant_id == parent.grant_id
    assert child.delegation_depth == 2
    assert child.max_delegation_depth == 3
    assert child.allow_patterns == ("data/*.csv",)
    assert child.deny_patterns == ("secrets/**",)
    assert child.write_prefixes == ("reports/charts/",)
    assert child.expires_at <= parent.expires_at

    with pytest.raises(GrantDelegationDenied, match="read pattern"):
        await ws.delegate(audience="chart-agent", allow_patterns=("**",))
    with pytest.raises(GrantDelegationDenied, match="write prefix"):
        await ws.delegate(
            audience="chart-agent",
            allow_patterns=("data/*.csv",),
            outputs_prefix="scratch/",
        )


# ---------------------------------------------------------------------------
# Two-agent demo: main calls graph in-process, hands a grant
# ---------------------------------------------------------------------------


class _GraphAgent(A2AAgent):
    name = "graph-agent"
    description = "Generates a chart from spreadsheet files"
    tools_used = ("matplotlib",)
    workspace_access = WorkspaceAccess.dynamic(
        max_files=8,
        allowed_modes=(WorkspaceMode.READ_ONLY,),
        deny_patterns=("secrets/**",),
    )

    @skill(description="Render a chart", tags=["visualization", "spreadsheet"])
    async def generate_dashboard(
        self, ctx: RunContext[NoAuth], prompt: str
    ) -> dict:
        # Workspace was attached by the runtime from the caller's grant.
        ws = ctx.workspace
        return {
            "prompt": prompt,
            "bucket": getattr(ws, "bucket", None),
            "deny_patterns": list(getattr(ws, "_access").deny_patterns),
        }


class _MainAgent(A2AAgent):
    name = "main-agent"
    description = "Orchestrates user files via discovered agents"

    @skill(description="Find a viz agent and delegate the chart")
    async def make_chart(self, ctx: RunContext[NoAuth], prompt: str) -> dict:
        hits = await ctx.discover.find_agents(tags=["visualization"])
        assert hits, "no graph agent in registry"
        graph = hits[0]

        token = await ctx.workspace.delegate(
            audience=graph.name,
            allow_patterns=("*.xlsx",),
            deny_patterns=("secrets/**",),
            outputs_prefix="charts/",
            ttl_seconds=300,
        )
        result = await ctx.call(
            graph.name, "generate_dashboard", args={"prompt": prompt}, grant=token
        )
        return {"called": graph.name, "out": result.result, "grant_id": result.grant_id}


def _build_a2a_router(
    agents: dict[str, A2AAgent], caller_workspace: LocalWorkspaceClient
):
    """In-memory router. Builds a callee-side ctx whose workspace is bounded
    by the inbound grant — same shape the HTTP server adapter does."""

    def factory(agent: A2AAgent, grant_token: str | None):
        ws = None
        if grant_token is not None:
            grant = verify_grant(grant_token)
            ws = LocalWorkspaceClient(
                files={
                    p: b
                    for p, b in caller_workspace._files.items()
                    if not any(p.startswith(d.rstrip("*").rstrip("/")) for d in grant.deny_patterns if d)
                },
                access=WorkspaceAccess.dynamic(
                    max_files=64,
                    allowed_modes=(WorkspaceMode.READ_ONLY,),
                    deny_patterns=tuple(grant.deny_patterns),
                ),
                bucket=grant.bucket,
                issuer=grant.audience,
            )
            ws.install_grant(grant)
        return LocalRunContext(auth=NoAuth(), workspace=ws)

    return InMemoryA2AClient(agents=agents, ctx_factory=factory)


async def test_main_agent_discovers_and_delegates_to_graph_agent():
    main = _MainAgent()
    graph = _GraphAgent()

    user_workspace = LocalWorkspaceClient(
        files={
            "sales.xlsx": b"q1,q2,q3\n10,20,30\n",
            "secrets/.env": b"NEVER",
        },
        access=WorkspaceAccess.dynamic(
            max_files=10,
            allowed_modes=(
                WorkspaceMode.READ_ONLY,
                WorkspaceMode.READ_WRITE_OVERLAY,
            ),
            deny_patterns=("secrets/**",),
        ),
        bucket="user-42-files",
        issuer="user-42",
    )

    discovery = InMemoryDiscovery(
        {graph.name: DiscoveredAgent(name=graph.name, url=None, card=graph.card())}
    )
    router = _build_a2a_router({graph.name: graph}, caller_workspace=user_workspace)

    out = await main.local_invoke(
        "make_chart",
        workspace=user_workspace,
        a2a=router,
        discover=discovery,
        prompt="weekly burn rate",
    )

    assert out["called"] == "graph-agent"
    assert out["out"]["bucket"] == "user-42-files"
    # Callee saw the deny patterns we minted in the grant
    assert "secrets/**" in out["out"]["deny_patterns"]
    # And the call was audit-tagged
    assert out["grant_id"]


async def test_ctx_subagents_lists_and_calls_with_narrowed_workspace_grant():
    graph = _GraphAgent()
    user_workspace = LocalWorkspaceClient(
        files={
            "sales.xlsx": b"q1,q2,q3\n10,20,30\n",
            "secrets/.env": b"NEVER",
        },
        access=WorkspaceAccess.dynamic(
            max_files=10,
            allowed_modes=(
                WorkspaceMode.READ_ONLY,
                WorkspaceMode.READ_WRITE_OVERLAY,
            ),
            deny_patterns=("secrets/**",),
        ),
        bucket="user-42-files",
        issuer="user-42",
    )
    discovery = InMemoryDiscovery(
        {graph.name: DiscoveredAgent(name=graph.name, url=None, card=graph.card())}
    )
    router = _build_a2a_router({graph.name: graph}, caller_workspace=user_workspace)
    ctx = LocalRunContext(
        auth=NoAuth(),
        workspace=user_workspace,
        a2a=router,
        discover=discovery,
    )

    listed = await ctx.subagents.list_subagents(tags=["visualization"])
    called = await ctx.subagents.call_subagent(
        "graph-agent",
        "generate_dashboard",
        args={"prompt": "weekly burn rate"},
        read_patterns=("*.xlsx",),
        deny_patterns=("secrets/**",),
        outputs_prefix="charts/",
        ttl_seconds=300,
    )

    assert listed[0]["name"] == "graph-agent"
    assert listed[0]["skills"][0]["name"] == "generate_dashboard"
    assert called["ok"] is True
    assert called["agent"] == "graph-agent"
    assert called["result"]["bucket"] == "user-42-files"
    assert "secrets/**" in called["result"]["deny_patterns"]
    assert called["grant_id"]


async def test_ctx_subagents_rejects_child_scope_wider_than_parent_grant():
    graph = _GraphAgent()
    user_workspace = LocalWorkspaceClient(
        files={
            "data/sales.csv": b"q1,q2,q3\n10,20,30\n",
            "secrets/.env": b"NEVER",
        },
        access=WorkspaceAccess.dynamic(
            max_files=10,
            allowed_modes=(WorkspaceMode.READ_ONLY,),
            deny_patterns=("secrets/**",),
        ),
        bucket="user-42-files",
        issuer="meta-agent",
    )
    parent, _ = mint_grant(
        issuer="user-42",
        audience="meta-agent",
        bucket="user-42-files",
        allow_patterns=("data/**",),
        deny_patterns=("secrets/**",),
        ttl_seconds=600,
    )
    user_workspace.install_grant(parent)
    discovery = InMemoryDiscovery(
        {graph.name: DiscoveredAgent(name=graph.name, url=None, card=graph.card())}
    )
    router = _build_a2a_router({graph.name: graph}, caller_workspace=user_workspace)
    ctx = LocalRunContext(
        auth=NoAuth(),
        workspace=user_workspace,
        a2a=router,
        discover=discovery,
    )

    called = await ctx.subagents.call_subagent(
        "graph-agent",
        "generate_dashboard",
        args={"prompt": "weekly burn rate"},
        read_patterns=("data/*.csv",),
        ttl_seconds=300,
    )
    assert called["ok"] is True

    with pytest.raises(GrantDelegationDenied, match="read pattern"):
        await ctx.subagents.call_subagent(
            "graph-agent",
            "generate_dashboard",
            args={"prompt": "weekly burn rate"},
            read_patterns=("**",),
        )
    _, sibling_token = mint_grant(
        issuer="user-42",
        audience="graph-agent",
        bucket="user-42-files",
        allow_patterns=("**",),
        ttl_seconds=300,
    )
    with pytest.raises(GrantDelegationDenied, match="not delegated"):
        await ctx.subagents.call_subagent(
            "graph-agent",
            "generate_dashboard",
            args={"prompt": "weekly burn rate"},
            grant=sibling_token,
        )


def test_subagent_tools_returns_list_and_call_tools():
    ctx = LocalRunContext(auth=NoAuth(), discover=InMemoryDiscovery({}))

    tools = subagent_tools(ctx)
    names = {getattr(tool, "name", getattr(tool, "__name__", "")) for tool in tools}

    assert {"list_subagents", "call_subagent"}.issubset(names)


async def test_no_grant_means_no_workspace_for_callee():
    """If the main agent didn't delegate, the callee can't touch any workspace."""
    from a2a_pack import SkillInvocationError

    class _Greedy(A2AAgent):
        name = "greedy"
        description = ""

        @skill()
        async def steal(self, ctx: RunContext[NoAuth]) -> str:
            return str(ctx.workspace.bucket)  # type: ignore[attr-defined]

    class _Caller(A2AAgent):
        name = "caller"
        description = ""

        @skill()
        async def go(self, ctx: RunContext[NoAuth]) -> str:
            r = await ctx.call("greedy", "steal", args={}, grant=None)
            return str(r.result)

    router = InMemoryA2AClient(
        agents={"greedy": _Greedy()},
        ctx_factory=lambda agent, grant: LocalRunContext(auth=NoAuth()),
    )
    discovery = InMemoryDiscovery({})
    with pytest.raises(SkillInvocationError) as ei:
        await _Caller().local_invoke("go", a2a=router, discover=discovery)
    # Trace back to the PermissionError: callee accessed ctx.workspace with
    # nothing bound, so the runtime denied it.
    chain = []
    err: BaseException | None = ei.value
    while err is not None:
        chain.append(err)
        err = err.__cause__
    assert any(isinstance(e, PermissionError) for e in chain)


async def test_ctx_call_forwards_control_plane_tracking_context():
    class _Callee(A2AAgent):
        name = "callee"
        description = ""

        @skill()
        async def who(self, ctx: RunContext[NoAuth]) -> dict:
            return {"jwt": ctx.cp_jwt, "url": ctx.cp_url}

    class _Caller(A2AAgent):
        name = "caller"
        description = ""

        @skill()
        async def go(self, ctx: RunContext[NoAuth]) -> dict:
            result = await ctx.call("callee", "who")
            return result.result

    router = InMemoryA2AClient(agents={"callee": _Callee()})
    ctx = LocalRunContext(auth=NoAuth(), a2a=router)
    ctx._cp_jwt = "jwt-123"  # noqa: SLF001
    ctx._cp_url = "http://control-plane"  # noqa: SLF001

    out = await _Caller().invoke("go", ctx)

    assert out == {"jwt": "jwt-123", "url": "http://control-plane"}


async def test_ctx_call_forwards_explicit_consumer_setup_to_child_context():
    class _Callee(A2AAgent):
        name = "callee"
        description = ""
        consumer_setup = ConsumerSetup.from_fields(
            ConsumerSetupField.secret("API_TOKEN"),
            ConsumerSetupField.config("REGION"),
        )

        @skill()
        async def read_setup(self, ctx: RunContext[NoAuth]) -> dict:
            return {
                "region": ctx.consumer_config("REGION"),
                "token": ctx.consumer_secret("API_TOKEN"),
            }

    class _Caller(A2AAgent):
        name = "caller"
        description = ""

        @skill()
        async def go(self, ctx: RunContext[NoAuth]) -> dict:
            result = await ctx.call(
                "callee",
                "read_setup",
                consumer_config={"REGION": "us-east"},
                consumer_secrets={"API_TOKEN": "tok-inline"},
            )
            return result.result

    router = InMemoryA2AClient(agents={"callee": _Callee()})
    ctx = LocalRunContext(auth=NoAuth(), a2a=router)

    out = await _Caller().invoke("go", ctx)

    assert out == {"region": "us-east", "token": "tok-inline"}


async def test_ctx_call_forwards_composition_budget_to_child_context():
    class _Callee(A2AAgent):
        name = "callee"
        description = ""

        @skill()
        async def inspect(self, ctx: RunContext[NoAuth]) -> dict:
            budget = ctx.composition_budget
            return {
                "run_id": budget.run_id,
                "stack": list(budget.stack),
                "remaining_calls": budget.remaining_calls,
            }

    class _Caller(A2AAgent):
        name = "caller"
        description = ""

        @skill()
        async def go(self, ctx: RunContext[NoAuth]) -> dict:
            result = await ctx.call("callee", "inspect")
            return result.result

    router = InMemoryA2AClient(agents={"callee": _Callee()})
    ctx = LocalRunContext(auth=NoAuth(), a2a=router)

    out = await _Caller().invoke("go", ctx)

    assert out["stack"] == ["caller", "callee"]
    assert out["remaining_calls"] == CompositionBudget.start("x").max_calls - 1
    assert any(event.kind == "composition_call_started" for event in ctx.events)
    assert any(event.kind == "composition_call_complete" for event in ctx.events)


def test_composition_budget_defaults_are_scaled_up() -> None:
    budget = CompositionBudget.start("caller")

    assert budget.max_depth == 40
    assert budget.max_calls == 320


async def test_ctx_call_enforces_composition_call_budget() -> None:
    class _Callee(A2AAgent):
        name = "callee"
        description = ""

        @skill()
        async def ok(self, ctx: RunContext[NoAuth]) -> str:
            return "ok"

    ctx = LocalRunContext(
        auth=NoAuth(),
        a2a=InMemoryA2AClient(agents={"callee": _Callee()}),
        composition_budget=CompositionBudget.start("caller", max_calls=1),
    )
    object.__setattr__(ctx, "_current_agent_name", "caller")

    assert (await ctx.call("callee", "ok")).result == "ok"
    with pytest.raises(CompositionLimitExceeded, match="call budget exhausted"):
        await ctx.call("callee", "ok")
    assert any(event.kind == "composition_limit" for event in ctx.events)


async def test_ctx_call_rejects_composition_cycle_before_transport() -> None:
    ctx = LocalRunContext(
        auth=NoAuth(),
        a2a=InMemoryA2AClient(agents={}),
        composition_budget=CompositionBudget.start("meta"),
    )
    object.__setattr__(ctx, "_current_agent_name", "meta")

    with pytest.raises(CompositionLimitExceeded, match="cycle"):
        await ctx.call("meta", "pursue")
