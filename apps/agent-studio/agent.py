"""agent-studio - bounded build/review/improve coordinator."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

import a2a_pack as a2a
from a2a_pack import (
    A2AAgent,
    AgentComposition,
    AgentGoal,
    AgentMemory,
    CompositionSubAgent,
    LLMProvisioning,
    MetaAgentManifest,
    NoAuth,
    Resources,
    RunContext,
    WorkspaceAccess,
    WorkspaceMode,
)

from agent_studio import AgentStudioCoordinator, AgentStudioSpecialists


class AgentStudioConfig(BaseModel):
    pass


class AgentStudio(A2AAgent[AgentStudioConfig, NoAuth]):
    name = "agent-studio"
    description = (
        "Plan, build, browser-test, repair, and distribute a managed A2A agent "
        "by coordinating Builder, Reviewer, and Code Editor specialists."
    )
    version = "0.1.31"

    config_model = AgentStudioConfig
    auth_model = NoAuth
    meta_agent_manifest = MetaAgentManifest(
        composition=AgentComposition(
            sub_agents=(
                CompositionSubAgent(
                    name="agent-builder",
                    skills=("build",),
                ),
                CompositionSubAgent(
                    name="agent-reviewer",
                    skills=("review",),
                    required=False,
                ),
                CompositionSubAgent(
                    name="code-editor-agent",
                    skills=("turn",),
                    required=False,
                ),
            ),
            planning="deterministic_dag",
            max_nodes=8,
            max_parallel=2,
            max_replans=0,
        ),
        goal=AgentGoal(
            objective=(
                "Plan, build, prove, and improve one managed A2A agent "
                "inside bounded user-approved budgets."
            ),
            success_criteria=(
                "managed agent exists with a live Agent Card and production proof",
                "critical reviewer findings are absent or block success",
                "every mutation is captured in the Agent Studio ledger",
            ),
            constraints=(
                "public publish remains opt-in",
                "no unbounded recursive agent creation",
                "code-editor push_on_failure is never enabled",
            ),
        ),
        memory=AgentMemory(
            tiers=("files",),
            namespace="agent-studio",
            scope="agent",
            retention="durable",
        ),
    )
    tools_used = ("agent-builder", "agent-reviewer", "code-editor-agent", "a2a-pack")
    llm_provisioning = LLMProvisioning.PLATFORM_OR_CALLER_PROVIDED
    wants_cp_jwt = True
    workspace_access = WorkspaceAccess.dynamic(
        allowed_modes=(WorkspaceMode.READ_ONLY, WorkspaceMode.READ_WRITE_OVERLAY),
    )
    resources = Resources(cpu="2", memory="2Gi", max_runtime_seconds=3600)

    @a2a.tool(
        description=(
            "Create a managed A2A agent from a goal, then evaluate its API, "
            "artifacts, real frontend, and distribution, review the source, "
            "and improve it within bounded iterations. "
            "Build is delegated through agent-builder, quality is checked "
            "through deterministic smoke calls and agent-reviewer, and "
            "bounded fixes go through code-editor-agent."
        ),
        tags=["agent-studio", "builder", "review", "code-editor", "meta"],
        stream=True,
        timeout_seconds=3600,
        cost_class="expensive",
        grant_mode="read_write_overlay",
        grant_allow_patterns=("agents/{name}/**",),
        grant_outputs_prefix="agents/{name}/.agent-studio/",
        grant_write_prefixes=(
            "agents/{name}/",
            "agents/{name}/.agent-studio/",
        ),
        grant_ttl_seconds=3600,
        grant_run_timeout_seconds=3540,
        grant_approval_timeout_seconds=180,
        grant_scope_approval_timeout_seconds=120,
    )
    async def create_agent(
        self,
        ctx: RunContext[NoAuth],
        name: str,
        goal: str,
        public: bool = False,
        max_iterations: int = 3,
        quality_bar: str = "standard",
        exercise_code_editor: bool = False,
        max_spend_cents: int = 500,
        app_spec_json: str = "{}",
        organization_slug: str = "",
    ) -> dict[str, Any]:
        normalized_quality_bar: Literal["standard", "high"] = (
            "high" if str(quality_bar).strip().lower() == "high" else "standard"
        )
        await ctx.emit_progress(
            f"agent-studio starting name={name} quality={normalized_quality_bar} "
            f"public={public} max_iterations={max_iterations}"
        )
        coordinator = AgentStudioCoordinator(AgentStudioSpecialists(ctx))
        report = await coordinator.run(
            ctx,
            name=name,
            goal=goal,
            public=public,
            max_iterations=max_iterations,
            quality_bar=normalized_quality_bar,
            exercise_code_editor=exercise_code_editor,
            max_spend_cents=max(100, min(int(max_spend_cents), 3000)),
            app_spec_json=app_spec_json,
            organization_slug=organization_slug,
        )
        await ctx.emit_progress(
            f"agent-studio finished name={name} status={report.status} "
            f"deployment_id={report.deployment_id or 'none'}"
        )
        return report.model_dump()

    @a2a.tool(
        description=(
            "Apply one owner-approved improvement proposal to an existing managed "
            "agent. Verifies source freshness, delegates the bounded patch to Code "
            "Editor, waits for the exact deployment, and reviews the result."
        ),
        tags=["agent-studio", "upgrade", "review", "code-editor", "approval"],
        stream=True,
        timeout_seconds=3600,
        cost_class="expensive",
        grant_mode="read_write_overlay",
        grant_allow_patterns=("agents/{name}/**",),
        grant_outputs_prefix="agents/{name}/.agent-studio/",
        grant_write_prefixes=(
            "agents/{name}/",
            "agents/{name}/.agent-studio/",
        ),
        grant_ttl_seconds=3600,
        grant_run_timeout_seconds=3540,
    )
    async def upgrade_agent(
        self,
        ctx: RunContext[NoAuth],
        name: str,
        idea: str,
        proposal_id: str = "",
        expected_head_sha: str = "",
        evidence_json: str = "{}",
    ) -> dict[str, Any]:
        import json

        try:
            evidence = json.loads(evidence_json or "{}")
        except json.JSONDecodeError:
            evidence = {"warning": "proposal evidence was not valid JSON"}
        if not isinstance(evidence, dict):
            evidence = {"value": evidence}
        await ctx.emit_progress(f"agent-studio upgrade starting name={name}")
        report = await AgentStudioCoordinator(AgentStudioSpecialists(ctx)).upgrade(
            ctx,
            name=name,
            idea=idea,
            proposal_id=proposal_id or None,
            expected_head_sha=expected_head_sha or None,
            evidence=evidence,
        )
        await ctx.emit_progress(
            f"agent-studio upgrade finished name={name} status={report.status}"
        )
        return report.model_dump()


AgentStudioAgent = AgentStudio
