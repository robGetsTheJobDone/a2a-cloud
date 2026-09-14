"""Example agent: full declarative surface + workspace negotiation.

Requires: nothing. Runs fully offline against an in-memory workspace — no
network, no LLM key, no deployed control plane. It prints the Agent Card, then
invokes ``research`` and prints the tool's streamed output, which names each
file the declared workspace policy let it see.

Run::

    cd sdk/a2a-pack
    PYTHONPATH=. python -m examples.research_agent
"""
from __future__ import annotations

import asyncio

from pydantic import BaseModel

import a2a_pack as a2a
from a2a_pack import (
    A2AAgent,
    AgentEvent,
    EgressPolicy,
    FileType,
    JWTAuth,
    Lifecycle,
    LocalRunContext,
    LocalWorkspaceClient,
    Resources,
    RunContext,
    State,
    WorkspaceAccess,
    WorkspaceMode,
)


class ResearchConfig(BaseModel):
    model: str = "qwen2.5-coder"


class ResearchSession(BaseModel):
    history: list[str] = []


class ResearchAgent(A2AAgent[ResearchConfig, JWTAuth]):
    name = "research-agent"
    description = "Researches questions across a session, with workspace access"

    config_model = ResearchConfig
    auth_model = JWTAuth

    lifecycle = Lifecycle.SESSION
    state = State.SESSION
    state_model = ResearchSession
    resources = Resources(cpu="1", memory="1Gi", max_runtime_seconds=900)
    egress = EgressPolicy(
        allow_internal_services=("litellm.llm.svc.cluster.local",),
    )
    tools_used = ("litellm",)
    required_secrets = ("LITELLM_API_KEY",)
    capabilities = {"streaming": True, "artifacts": True}

    workspace_access = WorkspaceAccess.dynamic(
        max_files=8,
        allowed_modes=(WorkspaceMode.READ_ONLY, WorkspaceMode.READ_WRITE_OVERLAY),
        deny_patterns=("secrets/**", "**/.env"),
    )

    @a2a.tool(scopes=["research:run"], stream=True, timeout_seconds=120)
    async def research(
        self,
        ctx: RunContext[JWTAuth],
        query: str,
        depth: int = 3,
    ) -> str:
        await ctx.emit_progress(f"researching {query!r} for {ctx.auth.sub}")

        view = await ctx.workspace.open_view(
            purpose=f"Research support for: {query}",
            hints=[query],
            file_types=[FileType.PYTHON, FileType.MARKDOWN],
            max_files=4,
            mode=WorkspaceMode.READ_ONLY,
        )
        for fm in view.files:
            await ctx.emit_text_delta(f"reading {fm.path}\n")

        return f"Researched {query!r} at depth {depth} ({len(view.files)} files referenced)"


async def main() -> None:
    agent = ResearchAgent()
    print(agent.card().model_dump_json(indent=2))
    print()

    workspace = LocalWorkspaceClient(
        files={
            "src/quantum.py": b"# notes on quantum computing",
            "docs/intro.md": b"# Quantum primer",
            "secrets/.env": b"DO_NOT_LEAK=1",
        },
        access=ResearchAgent.workspace_access,
    )
    # `research` declares stream=True and emits one text delta per file it was
    # allowed to open. `local_invoke` throws those away, so bind the context
    # directly and subscribe: the deltas are the whole point of the example.
    async def on_event(event: AgentEvent) -> None:
        if event.kind == "text_delta":
            print("  " + event.payload["text"].rstrip())

    ctx: LocalRunContext[JWTAuth] = LocalRunContext(
        auth=JWTAuth(sub="alice", scopes=["research:run"]),
        workspace=workspace,
        on_event=on_event,
    )

    print("=== files the workspace policy let `research` see ===")
    result = await agent.invoke("research", ctx, query="quantum computing")
    print(
        "  → secrets/.env is NOT in the list. workspace_access denies "
        "secrets/** and **/.env, so open_view never surfaced it."
    )
    print()
    print("RESULT:", result)


if __name__ == "__main__":
    asyncio.run(main())
