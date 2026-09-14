"""Language-neutral agent declaration DSL.

The DSL is the sidecar contract: target-language SDKs compile native
classes/interfaces/traits into this JSON shape, and the platform sidecar can
derive Agent Cards, MCP tools, runtime policy, and invoke routing without
importing target-language source.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .auth import APIKeyAuth, JWTAuth, NoAuth, PlatformUserAuth
from .card import AgentCard, SkillCard
from .consumer_setup import ConsumerSetup
from .runtime import AgentRuntime, MetaAgentManifest, SkillPolicy, TemplateLineage
from .workspace import WorkspaceAccess

if TYPE_CHECKING:
    from .agent import A2AAgent


AGENT_DSL_SCHEMA_VERSION = "2026-06-04"
AgentDslLanguage = Literal[
    "python",
    "typescript",
    "javascript",
    "go",
    "rust",
    "java",
    "dotnet",
]
AgentDslAuthStrategy = Literal[
    "public",
    "api_key",
    "platform_user",
    "jwt",
    "custom",
]


class AgentDslEntrypoint(BaseModel):
    """Native runtime entrypoint for a compiled agent declaration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    module: str | None = None
    class_name: str | None = None
    function: str | None = None
    command: tuple[str, ...] = ()

    @field_validator("module", "class_name", "function")
    @classmethod
    def _clean_optional_text(cls, value: str | None) -> str | None:
        cleaned = str(value or "").strip()
        return cleaned or None

    @field_validator("command", mode="before")
    @classmethod
    def _clean_command(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            return tuple(part for part in value.split() if part)
        if isinstance(value, (list, tuple)):
            return tuple(str(part) for part in value if str(part).strip())
        raise ValueError("entrypoint.command must be a string or list of strings")


class AgentDslSkill(BaseModel):
    """One callable skill compiled from a target-language declaration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str = ""
    handler: str
    tags: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()
    stream: bool = False
    policy: SkillPolicy = Field(default_factory=SkillPolicy)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]

    @field_validator("name", "handler")
    @classmethod
    def _require_text(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("must not be empty")
        return cleaned

    @field_validator("description")
    @classmethod
    def _clean_description(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("tags", "scopes", mode="before")
    @classmethod
    def _clean_string_tuple(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        raw = [value] if isinstance(value, str) else list(value)
        seen: set[str] = set()
        out: list[str] = []
        for item in raw:
            cleaned = str(item or "").strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            out.append(cleaned)
        return tuple(out)

    @field_validator("input_schema")
    @classmethod
    def _validate_input_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value.get("type") != "object":
            raise ValueError("skill input_schema must be a JSON object schema")
        if not isinstance(value.get("properties"), dict):
            raise ValueError("skill input_schema must define properties")
        required = value.get("required")
        if not isinstance(required, list) or not all(
            isinstance(item, str) for item in required
        ):
            raise ValueError("skill input_schema must define required as a string list")
        unknown_required = sorted(set(required) - set(value["properties"]))
        if unknown_required:
            raise ValueError(
                f"skill input_schema required fields are not properties: {unknown_required}"
            )
        return value

    @field_validator("output_schema")
    @classmethod
    def _validate_output_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            raise ValueError("skill output_schema must not be empty")
        return value


class AgentDslAuth(BaseModel):
    """Auth principal and sidecar resolution strategy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str
    strategy: AgentDslAuthStrategy
    principal_schema: dict[str, Any]
    resolver: str | None = None
    required: bool = True

    @field_validator("model")
    @classmethod
    def _require_model(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("auth model must not be empty")
        return cleaned

    @field_validator("resolver")
    @classmethod
    def _clean_resolver(cls, value: str | None) -> str | None:
        cleaned = str(value or "").strip()
        return cleaned or None


class AgentDsl(BaseModel):
    """Compiled agent declaration consumed by the common sidecar runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = AGENT_DSL_SCHEMA_VERSION
    language: AgentDslLanguage
    name: str
    description: str
    version: str
    entrypoint: AgentDslEntrypoint
    skills: tuple[AgentDslSkill, ...]
    capabilities: dict[str, Any] = Field(default_factory=dict)
    input_modes: tuple[str, ...] = ("application/json",)
    output_modes: tuple[str, ...] = ("application/json",)
    required_secrets: tuple[str, ...] = ()
    required_env: tuple[str, ...] = ()
    consumer_setup: ConsumerSetup = Field(default_factory=ConsumerSetup.none)
    runtime: AgentRuntime = Field(default_factory=AgentRuntime)
    template_lineage: TemplateLineage | None = None
    meta_agent_manifest: MetaAgentManifest | None = None
    state_schema: dict[str, Any] | None = None
    workspace_access: WorkspaceAccess = Field(default_factory=WorkspaceAccess.none)
    config_schema: dict[str, Any] | None = None
    auth: AgentDslAuth
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("schema_version")
    @classmethod
    def _clean_schema_version(cls, value: str) -> str:
        return str(value or "").strip() or AGENT_DSL_SCHEMA_VERSION

    @field_validator("name", "version")
    @classmethod
    def _require_text(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("must not be empty")
        return cleaned

    @field_validator("description")
    @classmethod
    def _clean_description(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("skills")
    @classmethod
    def _require_skills(cls, value: tuple[AgentDslSkill, ...]) -> tuple[AgentDslSkill, ...]:
        if not value:
            raise ValueError("agent DSL requires at least one skill")
        names = [skill.name for skill in value]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate skill names: {duplicates}")
        return value

    def to_agent_card(self) -> AgentCard:
        """Project the internal DSL to the public discovery card."""

        capabilities = dict(self.capabilities)
        capabilities.setdefault(
            "a2a_pack",
            {
                "package": "a2a-pack",
                "dsl_schema_version": self.schema_version,
                "language": self.language,
            },
        )
        capabilities.setdefault(
            "mcp",
            {
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
            },
        )
        if self.meta_agent_manifest is not None:
            manifest_payload = self.meta_agent_manifest.public_payload()
            if manifest_payload:
                capabilities["meta_agent"] = manifest_payload
        return AgentCard(
            name=self.name,
            description=self.description,
            version=self.version,
            skills=[
                SkillCard(
                    id=skill.name,
                    name=skill.name,
                    description=skill.description,
                    tags=list(skill.tags),
                    scopes=list(skill.scopes),
                    stream=skill.stream,
                    policy=skill.policy,
                    input_schema=skill.input_schema,
                    output_schema=skill.output_schema,
                )
                for skill in self.skills
            ],
            capabilities=capabilities,
            input_modes=list(self.input_modes),
            output_modes=list(self.output_modes),
            required_secrets=list(self.required_secrets),
            required_env=list(self.required_env),
            consumer_setup=self.consumer_setup,
            runtime=self.runtime,
            template_lineage=self.template_lineage,
            state_schema=self.state_schema,
            workspace_access=self.workspace_access,
            mcp_endpoint="/mcp",
            connector_mcp_endpoint="/connector-mcp",
            mcp_endpoints=capabilities["mcp"],
        )


def compile_agent_to_dsl(
    agent_cls: type["A2AAgent"],
    *,
    language: AgentDslLanguage = "python",
    entrypoint: str | AgentDslEntrypoint | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> AgentDsl:
    """Compile a Python ``A2AAgent`` declaration to the sidecar DSL."""

    parsed_entrypoint = (
        entrypoint
        if isinstance(entrypoint, AgentDslEntrypoint)
        else _entrypoint_from_string(entrypoint)
    )
    state_schema = (
        agent_cls.state_model.model_json_schema()
        if agent_cls.state_model is not None
        else None
    )
    lineage = getattr(agent_cls, "template_lineage", None)
    template_lineage = (
        lineage
        if isinstance(lineage, TemplateLineage) and lineage.enabled
        else None
    )
    manifest = getattr(agent_cls, "meta_agent_manifest", None)
    meta_agent_manifest = (
        manifest
        if isinstance(manifest, MetaAgentManifest) and manifest.enabled
        else None
    )
    return AgentDsl(
        language=language,
        name=agent_cls.name,
        description=agent_cls.description,
        version=agent_cls.version,
        entrypoint=parsed_entrypoint,
        skills=tuple(
            AgentDslSkill(
                name=spec.name,
                description=spec.description,
                handler=getattr(spec.handler, "__name__", spec.name),
                tags=spec.tags,
                scopes=spec.scopes,
                stream=spec.stream,
                policy=spec.policy,
                input_schema=spec.input_schema,
                output_schema=spec.output_schema,
            )
            for spec in agent_cls._skills.values()
        ),
        capabilities=dict(agent_cls.capabilities),
        input_modes=tuple(agent_cls.input_modes),
        output_modes=tuple(agent_cls.output_modes),
        required_secrets=tuple(agent_cls.required_secrets),
        required_env=tuple(agent_cls.required_env),
        consumer_setup=agent_cls.consumer_setup,
        runtime=agent_cls.runtime(),
        template_lineage=template_lineage,
        meta_agent_manifest=meta_agent_manifest,
        state_schema=state_schema,
        workspace_access=agent_cls.workspace_access,
        config_schema=agent_cls.config_model.model_json_schema(),
        auth=_compile_auth(agent_cls),
        metadata=dict(metadata or {}),
    )


def _entrypoint_from_string(entrypoint: str | None) -> AgentDslEntrypoint:
    clean = str(entrypoint or "").strip()
    if not clean:
        return AgentDslEntrypoint()
    if ":" not in clean:
        return AgentDslEntrypoint(module=clean)
    module, class_name = clean.split(":", 1)
    return AgentDslEntrypoint(module=module.strip(), class_name=class_name.strip())


def _compile_auth(agent_cls: type["A2AAgent"]) -> AgentDslAuth:
    auth_model = agent_cls.auth_model
    resolver = getattr(agent_cls, "auth_resolver", None)
    if auth_model is NoAuth:
        strategy: AgentDslAuthStrategy = "public"
        required = False
    elif auth_model is APIKeyAuth:
        strategy = "api_key"
        required = True
    elif auth_model is PlatformUserAuth:
        strategy = "platform_user"
        required = True
    elif auth_model is JWTAuth:
        strategy = "jwt"
        required = True
    else:
        strategy = "custom"
        required = True
    return AgentDslAuth(
        model=f"{auth_model.__module__}.{auth_model.__qualname__}",
        strategy=strategy,
        principal_schema=auth_model.model_json_schema(),
        resolver=_resolver_ref(resolver),
        required=required,
    )


def _resolver_ref(resolver: Any) -> str | None:
    if resolver is None:
        return None
    cls = type(resolver)
    return f"{cls.__module__}.{cls.__qualname__}"
