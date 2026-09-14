"""Worked meta-agent example: manifest, DAG, sub-agents, and memory.

This runs entirely in-process so you can inspect the mechanics without a
deployed control plane. The meta-agent still uses the same inherited ``pursue``
tool that generated/deployed meta-agents use in production:

* the manifest declares callable child agents, goal, and memory policy
* the planner emits a raw-skill DAG
* the SDK validates the DAG against the manifest
* execution calls child agents through ``ctx.call``
* the run writes a durable memory note under ``memory/...``

Requires: nothing. Runs fully offline — the planner is a deterministic
stand-in for the hosted LLM, so no LLM key, network, or control plane is
needed.

Run::

    cd sdk/a2a-pack
    PYTHONPATH=. python -m examples.meta_agent_research
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import a2a_pack as a2a
from a2a_pack import (
    A2AAgent,
    AgentEvent,
    DiscoveredAgent,
    InMemoryA2AClient,
    InMemoryDiscovery,
    LocalRunContext,
    LocalWorkspaceClient,
    MetaAgent,
    MetaAgentManifest,
    NoAuth,
    RunContext,
    WorkspaceAccess,
    WorkspaceMode,
)


class SearchAgent(A2AAgent):
    name = "search-agent"
    description = "Finds source material for a research question."
    version = "0.1.0"

    @a2a.tool(description="Search for source material.", tags=["research", "search"])
    async def search(
        self,
        ctx: RunContext[NoAuth],
        topic: str,
        prior_findings: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        await ctx.emit_progress(f"searching sources for {topic!r}")
        budget = ctx.composition_budget
        prior_note_count = len(prior_findings or [])
        sources = [
            {
                "title": "Protocol adoption notes",
                "url": "https://example.invalid/protocol-adoption",
                "signal": "teams want interoperable agents with explicit cards",
            },
            {
                "title": "Runtime safety notes",
                "url": "https://example.invalid/runtime-safety",
                "signal": "bounded DAG execution and grants keep fan-out controlled",
            },
            {
                "title": "Memory design notes",
                "url": "https://example.invalid/memory-design",
                "signal": "durable memory lets repeat runs reuse prior context",
            },
        ]
        return {
            "topic": topic,
            "sources": sources,
            "prior_note_count": prior_note_count,
            "composition": {
                "stack": list(budget.stack),
                "remaining_calls": budget.remaining_calls,
            },
        }


class SummarizerAgent(A2AAgent):
    name = "summarizer-agent"
    description = "Turns source material into an executive brief."
    version = "0.1.0"

    @a2a.tool(description="Summarize source material.", tags=["research", "writing"])
    async def summarize(
        self,
        ctx: RunContext[NoAuth],
        topic: str,
        sources: list[dict[str, Any]],
    ) -> dict[str, Any]:
        await ctx.emit_progress(f"summarizing {len(sources)} sources")
        signals = [str(item.get("signal")) for item in sources]
        return {
            "topic": topic,
            "summary": (
                f"{topic}: " + " ".join(signals)
            ),
            "bullets": signals,
            "source_count": len(sources),
        }


class ChartAgent(A2AAgent):
    name = "chart-agent"
    description = "Builds a simple chart spec from research output."
    version = "0.1.0"

    @a2a.tool(description="Render a chart spec.", tags=["charting", "visualization"])
    async def render_chart(
        self,
        ctx: RunContext[NoAuth],
        title: str,
        summary: str,
        sources: list[dict[str, Any]],
    ) -> dict[str, Any]:
        await ctx.emit_progress("building chart spec")
        return {
            "chart_path": "outputs/charts/research-signals.json",
            "spec": {
                "title": title,
                "bars": [
                    {"label": item["title"], "value": index + 1}
                    for index, item in enumerate(sources)
                ],
                "caption": summary[:180],
            },
        }


class ResearchMetaAgent(MetaAgent):
    name = "research-meta"
    description = "Plans a research DAG over search, writing, and chart agents."
    version = "0.1.0"

    meta_agent_manifest = MetaAgentManifest.from_mapping(
        {
            "composition": {
                "planning": "llm_dag",
                "max_nodes": 4,
                "max_parallel": 2,
                "max_replans": 1,
                "sub_agents": [
                    {"name": "search-agent", "skills": ["search"]},
                    {"name": "summarizer-agent", "skills": ["summarize"]},
                    {
                        "name": "chart-agent",
                        "skills": ["render_chart"],
                        "required": False,
                    },
                ],
            },
            "goal": {
                "objective": "Create a sourced brief about composable agents.",
                "success_criteria": [
                    "brief cites source material",
                    "chart spec is produced",
                ],
            },
            "memory": {
                "tiers": ["files"],
                "namespace": "research-brief",
            },
        }
    )

    async def planner_llm_call(
        self,
        ctx: RunContext[NoAuth],
        messages: list[dict[str, str]],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Deterministic stand-in for the hosted LLM planner.

        Production generated meta-agents call the LLM through ``ctx.llm``.
        This local example returns the same JSON shape directly so it is
        repeatable in CI and on a laptop.
        """

        visible = await ctx.subagents.list_subagents(limit=10)
        await ctx.emit_event(
            AgentEvent(
                kind="example_subagent_catalog",
                payload={
                    "count": len(visible),
                    "agents": [item["name"] for item in visible],
                },
            )
        )
        goal = str(state.get("goal") or type(self).manifest().goal.objective)
        prior = (state.get("memory") or {}).get("relevant") or []
        return {
            "goal": goal,
            "nodes": [
                {
                    "id": "search",
                    "agent": "search-agent",
                    "skill": "search",
                    "args": {
                        "topic": goal,
                        "prior_findings": prior,
                    },
                },
                {
                    "id": "summarize",
                    "agent": "summarizer-agent",
                    "skill": "summarize",
                    "deps": ["search"],
                    "args": {
                        "topic": goal,
                        "sources": "{{ nodes.search.result.sources }}",
                    },
                },
                {
                    "id": "chart",
                    "agent": "chart-agent",
                    "skill": "render_chart",
                    "deps": ["search", "summarize"],
                    "args": {
                        "title": "Research signal coverage",
                        "summary": "{{ nodes.summarize.result.summary }}",
                        "sources": "{{ nodes.search.result.sources }}",
                    },
                },
            ],
        }


def _discovery(agents: dict[str, A2AAgent]) -> InMemoryDiscovery:
    return InMemoryDiscovery(
        {
            name: DiscoveredAgent(name=name, url=None, card=agent.card())
            for name, agent in agents.items()
        }
    )


async def main() -> None:
    child_agents: dict[str, A2AAgent] = {
        "search-agent": SearchAgent(),
        "summarizer-agent": SummarizerAgent(),
        "chart-agent": ChartAgent(),
    }
    discovery = _discovery(child_agents)

    def child_context(agent: A2AAgent, grant_token: str | None) -> LocalRunContext:
        return LocalRunContext(auth=NoAuth(), task_id=f"child-{type(agent).name}")

    a2a_client = InMemoryA2AClient(agents=child_agents, ctx_factory=child_context)
    workspace = LocalWorkspaceClient(
        files={},
        access=WorkspaceAccess.dynamic(
            max_files=64,
            allowed_modes=(
                WorkspaceMode.READ_ONLY,
                WorkspaceMode.READ_WRITE_OVERLAY,
            ),
        ),
        bucket="user-42-files",
        issuer=ResearchMetaAgent.name,
    )

    meta = ResearchMetaAgent()
    result = await meta.local_invoke(
        "pursue",
        auth=NoAuth(),
        workspace=workspace,
        a2a=a2a_client,
        discover=discovery,
        goal="Create a sourced brief about composable A2A meta-agents.",
        success_criteria=["brief cites sources", "chart spec is produced"],
    )

    compact = {
        "status": result["status"],
        "summary": result["summary"],
        "attempts": result["attempts"],
        "nodes": [
            {
                "id": node["node_id"],
                "agent": node["agent"],
                "skill": node["skill"],
                "ok": node["ok"],
                "summary": node["summary"],
            }
            for node in result["result"]["nodes"]
        ],
    }
    print(json.dumps(compact, indent=2))
    print("\nMemory files:")
    for path in sorted(workspace.iter_paths()):
        if path.startswith("memory/"):
            print(f"- {path}")


if __name__ == "__main__":
    asyncio.run(main())
