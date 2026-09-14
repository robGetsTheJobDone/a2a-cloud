from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, model_serializer

from . import __version__ as A2A_PACK_VERSION
from .consumer_setup import ConsumerSetup
from .runtime import AgentRuntime, SkillPolicy, TemplateLineage
from .workspace import WorkspaceAccess

if TYPE_CHECKING:
    from .agent import A2AAgent


class SkillCard(BaseModel):
    """Public description of a single skill, shaped for the A2A spec."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    description: str
    tags: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)
    stream: bool = False
    policy: SkillPolicy = Field(default_factory=SkillPolicy)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]


class AgentCard(BaseModel):
    """Public description of an agent.

    Mirrors the A2A Agent Card spec: identity, capabilities, IO modes, and
    the catalog of skills the agent advertises.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    version: str
    skills: list[SkillCard]
    capabilities: dict[str, Any] = Field(default_factory=dict)
    input_modes: list[str] = Field(default_factory=lambda: ["application/json"])
    output_modes: list[str] = Field(default_factory=lambda: ["application/json"])
    required_secrets: list[str] = Field(default_factory=list)
    required_env: list[str] = Field(default_factory=list)
    consumer_setup: ConsumerSetup = Field(default_factory=ConsumerSetup.none)
    runtime: AgentRuntime = Field(default_factory=AgentRuntime)
    template_lineage: TemplateLineage | None = None
    state_schema: dict[str, Any] | None = None
    workspace_access: WorkspaceAccess = Field(default_factory=WorkspaceAccess.none)
    mcp_endpoint: str = "/mcp"
    connector_mcp_endpoint: str = "/connector-mcp"
    mcp_endpoints: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_agent(cls, agent: "A2AAgent") -> "AgentCard":
        agent_cls = type(agent)
        skills = [
            SkillCard(
                id=spec.name,
                name=spec.name,
                description=spec.description,
                tags=list(spec.tags),
                scopes=list(spec.scopes),
                stream=spec.stream,
                policy=spec.policy,
                input_schema=spec.input_schema,
                output_schema=spec.output_schema,
            )
            for spec in agent.skills.values()
        ]
        state_schema = (
            agent_cls.state_model.model_json_schema()
            if agent_cls.state_model is not None
            else None
        )
        capabilities = dict(agent_cls.capabilities)
        capabilities["a2a_pack"] = {
            "package": "a2a-pack",
            "version": A2A_PACK_VERSION,
        }
        manifest = getattr(agent_cls, "meta_agent_manifest", None)
        manifest_payload = (
            manifest.public_payload()
            if hasattr(manifest, "public_payload")
            else {}
        )
        if manifest_payload:
            capabilities["meta_agent"] = manifest_payload
        capabilities["mcp"] = {
            "standard": {
                "path": "/mcp",
                "intended_for": "code_sdk_clients",
            },
            "connector": {
                "path": "/connector-mcp",
                "intended_for": ["chatgpt", "claude", "hosted_connectors"],
                "supports_async_jobs": True,
                "supports_structured_interrupts": True,
                "poll_tool": "job_result",
                "resume_tool": "submit_interaction",
                "instructions": (
                    "Use this URL for hosted connector UIs. Use /mcp for "
                    "normal code or SDK MCP clients."
                ),
            },
        }
        lineage = getattr(agent_cls, "template_lineage", None)
        template_lineage = (
            lineage
            if isinstance(lineage, TemplateLineage) and lineage.enabled
            else None
        )
        return cls(
            name=agent_cls.name,
            description=agent_cls.description,
            version=agent_cls.version,
            skills=skills,
            capabilities=capabilities,
            input_modes=list(agent_cls.input_modes),
            output_modes=list(agent_cls.output_modes),
            required_secrets=list(agent_cls.required_secrets),
            required_env=list(agent_cls.required_env),
            consumer_setup=agent_cls.consumer_setup,
            runtime=agent_cls.runtime(),
            template_lineage=template_lineage,
            state_schema=state_schema,
            workspace_access=agent_cls.workspace_access,
            mcp_endpoint="/mcp",
            connector_mcp_endpoint="/connector-mcp",
            mcp_endpoints=capabilities["mcp"],
        )

    @model_serializer(mode="wrap")
    def _dump_without_empty_template_lineage(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        if data.get("template_lineage") is None:
            data.pop("template_lineage", None)
        return data
