"""Declarative runtime/deployment metadata.

These types describe *how* the platform should run an agent: lifecycle,
state needs, isolation level, resource budget, egress policy. They are
read by the deployer and by the registry; agent code itself should not
depend on which runtime is selected.
"""
from __future__ import annotations

import re
import warnings
from enum import Enum
from typing import Any, Literal, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    PositiveInt,
    field_validator,
    model_serializer,
    model_validator,
)


class Lifecycle(str, Enum):
    """How long an instance of the agent process lives."""

    EPHEMERAL = "ephemeral"  # spawned per-invocation, torn down after
    SESSION = "session"  # spawned per-session, kept until the session ends
    WARM = "warm"  # long-running service, multiplexed across callers


class RuntimeAvailability(str, Enum):
    """Developer-facing availability intent for hosted deployments."""

    ON_DEMAND = "on_demand"
    ALWAYS_ON = "always_on"


class State(str, Enum):
    """What kind of state the agent retains between invocations."""

    NONE = "none"  # purely functional
    SESSION = "session"  # in-memory state for the lifetime of a session
    DURABLE = "durable"  # persisted across restarts (storage required)


class Sandbox(str, Enum):
    """Isolation level. The platform always runs agents under microsandbox.

    Modeled as an enum (rather than a constant) so the wire format stays
    stable if more isolation tiers are added later, but only one value is
    currently valid: every agent runs in a microvm-class sandbox.
    """

    MICROSANDBOX = "microsandbox"


class Resources(BaseModel):
    """Resource budget hint for the deployer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cpu: str = "100m"  # k8s-style CPU spec, e.g. "500m", "2"
    memory: str = "256Mi"  # k8s-style memory, e.g. "512Mi", "4Gi"
    gpu: NonNegativeInt = 0
    max_runtime_seconds: PositiveInt = 600


class SkillPolicy(BaseModel):
    """Per-skill operational policy advertised on the Agent Card."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timeout_seconds: float | None = None
    idempotent: bool = False
    max_retries: NonNegativeInt = 0
    cost_class: str | None = None  # informational, e.g. "cheap" / "expensive"
    # When True, the skill is allowed to call ``ctx.request_scope(...)`` to
    # negotiate additional workspace access mid-execution. Off by default —
    # platform refuses scope negotiation otherwise.
    allow_scope_expansion: bool = False

    # Optional first-grant hints. The caller remains the authority: it may
    # clamp, narrow, deny, or ignore these values before minting a grant.
    grant_mode: str | None = None
    grant_allow_patterns: tuple[str, ...] = ()
    grant_deny_patterns: tuple[str, ...] = ()
    grant_outputs_prefix: str | None = None
    grant_write_prefixes: tuple[str, ...] = ()
    grant_ttl_seconds: PositiveInt | None = None
    grant_run_timeout_seconds: PositiveInt | None = None
    grant_approval_timeout_seconds: PositiveInt | None = None
    grant_scope_approval_timeout_seconds: PositiveInt | None = None


class EgressPolicy(BaseModel):
    """What external hosts the agent is allowed to talk to."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allow_hosts: tuple[str, ...] = ()
    allow_internal_services: tuple[str, ...] = ()  # e.g. cluster service DNS
    deny_internet_by_default: bool = True


EndpointMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
_ENDPOINT_PATH_RE = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*$")
_ENDPOINT_ARG_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_RESERVED_ENDPOINT_PREFIXES = (
    "/.well-known",
    "/_a2a",
    "/answers",
    "/auth",
    "/connector-mcp",
    "/extendedAgentCard",
    "/healthz",
    "/input-requests",
    "/invoke",
    "/mcp",
    "/message",
    "/scope-denials",
    "/scope-grants",
    "/tasks",
)


class AgentEndpoint(BaseModel):
    """Raw HTTP endpoint adapter exposed by the runtime.

    The endpoint path is not a skill URL. The runtime accepts the provider's
    native HTTP request, maps it into handler arguments, and dispatches to the
    named skill through the normal agent execution path.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str | None = None
    path: str
    methods: tuple[EndpointMethod, ...] = ("POST",)
    skill: str
    body_arg: str = "body"
    headers_arg: str | None = None
    query_arg: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _aliases(cls, value: Any) -> Any:
        if isinstance(value, dict):
            raw = dict(value)
            if "methods" not in raw and "method" in raw:
                raw["methods"] = raw.pop("method")
            if "skill" not in raw and "target" in raw:
                raw["skill"] = raw.pop("target")
            return raw
        return value

    @field_validator("name")
    @classmethod
    def _clean_optional_name(cls, value: str | None) -> str | None:
        cleaned = str(value or "").strip()
        return cleaned or None

    @field_validator("skill", "body_arg")
    @classmethod
    def _require_text(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("must not be empty")
        return cleaned

    @field_validator("headers_arg", "query_arg")
    @classmethod
    def _clean_optional_arg(cls, value: str | None) -> str | None:
        cleaned = str(value or "").strip()
        return cleaned or None

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if not _ENDPOINT_PATH_RE.fullmatch(cleaned):
            raise ValueError("endpoint path must be an absolute safe URL path")
        if "//" in cleaned:
            raise ValueError("endpoint path must not contain empty segments")
        if any(
            cleaned == prefix
            or cleaned.startswith(f"{prefix}/")
            or cleaned.startswith(f"{prefix}:")
            for prefix in _RESERVED_ENDPOINT_PREFIXES
        ):
            raise ValueError(f"endpoint path {cleaned!r} uses a reserved A2A path")
        return cleaned

    @field_validator("methods", mode="before")
    @classmethod
    def _clean_methods(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            raw_items = ["POST"]
        elif isinstance(value, str):
            raw_items = [value]
        elif isinstance(value, (list, tuple, set)):
            raw_items = list(value)
        else:
            raise ValueError("endpoint methods must be a string or list of strings")
        seen: set[str] = set()
        out: list[str] = []
        for item in raw_items:
            method = str(item or "").strip().upper()
            if not method or method in seen:
                continue
            seen.add(method)
            out.append(method)
        return tuple(out or ["POST"])

    @model_validator(mode="after")
    def _validate_args(self) -> "AgentEndpoint":
        for field_name in ("body_arg", "headers_arg", "query_arg"):
            value = getattr(self, field_name)
            if value is not None and not _ENDPOINT_ARG_RE.fullmatch(value):
                raise ValueError(f"endpoint {field_name} must be a valid argument name")
        return self


class LLMProvisioning(str, Enum):
    """Where the agent's LLM credentials come from.

    Drives both runtime behaviour (``ctx.llm`` resolution) and what the
    marketplace can charge the caller.
    """

    PLATFORM = "platform"
    """Use the caller's saved LLM credential supplied by the platform.
    Authors don't read provider keys directly; the platform forwards
    ``ctx.llm`` from the user's configured credential."""

    PLATFORM_OR_CALLER_PROVIDED = "platform_or_caller_provided"
    """Backwards-compatible mixed mode. With the platform default key removed,
    this resolves to the caller's saved LLM credential."""

    CALLER_PROVIDED = "caller_provided"
    """Use the caller's own LLM credentials, forwarded by the CP in the
    invoke body. The author's per-call price covers the skill IP /
    compute only; the LLM bill goes to the caller's provider directly."""

    AGENT_BYOK = "agent_byok"
    """Author brings their own LLM key (set via env / Secret). The
    per-call price typically includes a markup on the LLM cost."""


class AccountAccess(BaseModel):
    """Account-gated, platform-funded trial policy for an agent.

    When enabled, callers must have an A2A Cloud account. The platform funds
    up to ``platform_skill_calls`` invocations for each account and agent;
    subsequent invocations use that account's saved LLM credential.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    required: bool = False
    platform_skill_calls: NonNegativeInt = 0
    after_trial: Literal["byok"] = "byok"

    @property
    def enabled(self) -> bool:
        return self.required

    @model_validator(mode="after")
    def _trial_requires_an_account(self) -> "AccountAccess":
        if self.platform_skill_calls and not self.required:
            raise ValueError(
                "account access must be required when platform_skill_calls is non-zero"
            )
        return self


TemplateUpdatePolicy = Literal["none", "notify", "propose", "auto_patch"]


class TemplateLineage(BaseModel):
    """Opt-in source-template lineage and update policy.

    Agents generated from templates can publish this on their Agent Card so
    platforms can detect available template updates and decide how to handle
    them. The policy is intentionally advisory: callers/platforms remain the
    authority for whether an update is proposed, applied, reviewed, or denied.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "2026-06-02"
    template_ref: str | None = None
    template_version: str | None = None
    template_digest: str | None = None
    source_agent: str | None = None
    source_agent_version: str | None = None
    source_repo_url: str | None = None
    source_revision: str | None = None
    instance_id: str | None = None
    instance_version: str | None = None
    update_policy: TemplateUpdatePolicy = "none"
    update_channel: str | None = None
    migration_skill: str | None = None

    @classmethod
    def none(cls) -> "TemplateLineage":
        return cls()

    @property
    def enabled(self) -> bool:
        return any(
            (
                self.template_ref,
                self.template_version,
                self.template_digest,
                self.source_agent,
                self.source_agent_version,
                self.source_repo_url,
                self.source_revision,
                self.instance_id,
                self.instance_version,
                self.update_policy != "none",
                self.update_channel,
                self.migration_skill,
            )
        )

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | str | None) -> "TemplateLineage":
        if data is None:
            return cls.none()
        if isinstance(data, str):
            return cls(template_ref=data)
        if isinstance(data, Mapping):
            return cls.model_validate(dict(data))
        raise ValueError("template_lineage must be a mapping or template ref string")

    @field_validator(
        "template_ref",
        "template_version",
        "template_digest",
        "source_agent",
        "source_agent_version",
        "source_repo_url",
        "source_revision",
        "instance_id",
        "instance_version",
        "update_channel",
        "migration_skill",
    )
    @classmethod
    def _clean_optional_text(cls, value: str | None) -> str | None:
        cleaned = str(value or "").strip()
        return cleaned or None

    @field_validator("schema_version")
    @classmethod
    def _clean_schema_version(cls, value: str) -> str:
        return str(value or "").strip() or "2026-06-02"

    @field_validator("update_policy", mode="before")
    @classmethod
    def _clean_update_policy(cls, value: Any) -> str:
        cleaned = str(value or "none").strip().lower().replace("-", "_")
        return cleaned or "none"

    @model_validator(mode="after")
    def _require_source_for_updates(self) -> "TemplateLineage":
        if self.update_policy != "none" and not (self.template_ref or self.source_agent):
            raise ValueError(
                "template lineage update_policy requires template_ref or source_agent"
            )
        return self

    @model_serializer(mode="wrap")
    def _dump_without_empty_fields(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        return {key: value for key, value in data.items() if value is not None}


class CompositionSubAgent(BaseModel):
    """One callable dependency declared by a composable/meta agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str | None = None
    tag: str | None = None
    version: str | None = None
    skills: tuple[str, ...] = ()
    default_args: dict[str, Any] = Field(default_factory=dict)
    required: bool = True

    @field_validator("name", "tag", "version")
    @classmethod
    def _clean_optional_text(cls, value: str | None) -> str | None:
        cleaned = str(value or "").strip()
        return cleaned or None

    @field_validator("skills", mode="before")
    @classmethod
    def _clean_skills(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            raw_items = [value]
        elif isinstance(value, (list, tuple, set)):
            raw_items = list(value)
        else:
            raise ValueError("skills must be a string or list of strings")
        seen: set[str] = set()
        out: list[str] = []
        for item in raw_items:
            skill = str(item or "").strip()
            if not skill or skill in seen:
                continue
            seen.add(skill)
            out.append(skill)
        return tuple(out)

    @model_validator(mode="after")
    def _require_binding(self) -> "CompositionSubAgent":
        if not self.name and not self.tag:
            raise ValueError("composition sub-agent requires name or tag")
        return self

    def public_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"required": self.required}
        if self.name:
            payload["name"] = self.name
        if self.tag:
            payload["tag"] = self.tag
        if self.version:
            payload["version"] = self.version
        if self.skills:
            payload["skills"] = list(self.skills)
        if self.default_args:
            payload["default_arg_keys"] = sorted(str(key) for key in self.default_args)
        return payload


class AgentComposition(BaseModel):
    """Declarative composition block from ``a2a.yaml``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sub_agents: tuple[CompositionSubAgent, ...] = ()
    planning: Literal["llm_dag", "deterministic_dag"] = "llm_dag"
    max_nodes: PositiveInt = 8
    max_parallel: PositiveInt = 3
    max_replans: NonNegativeInt = 1

    @model_validator(mode="before")
    @classmethod
    def _aliases(cls, value: Any) -> Any:
        if isinstance(value, (list, tuple)):
            return {"sub_agents": value}
        if isinstance(value, dict):
            raw = dict(value)
            if "sub_agents" not in raw:
                for alias in ("agents", "children"):
                    if alias in raw:
                        raw["sub_agents"] = raw.pop(alias)
                        break
            return raw
        return value

    def public_payload(self) -> dict[str, Any]:
        return {
            "planning": self.planning,
            "max_nodes": self.max_nodes,
            "max_parallel": self.max_parallel,
            "max_replans": self.max_replans,
            "sub_agents": [item.public_payload() for item in self.sub_agents],
        }


class AgentGoal(BaseModel):
    """Durable objective advertised by a composable/meta agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    objective: str = ""
    success_criteria: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()

    @field_validator("objective")
    @classmethod
    def _clean_objective(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("success_criteria", "constraints", mode="before")
    @classmethod
    def _clean_string_tuple(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            raw_items = [value]
        elif isinstance(value, (list, tuple, set)):
            raw_items = list(value)
        else:
            raise ValueError("must be a string or list of strings")
        return tuple(str(item).strip() for item in raw_items if str(item or "").strip())

    def public_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.objective:
            payload["objective"] = self.objective
        if self.success_criteria:
            payload["success_criteria"] = list(self.success_criteria)
        if self.constraints:
            payload["constraints"] = list(self.constraints)
        return payload


MemoryTier = Literal["files", "kv", "vector"]
DatabaseEngine = Literal["postgres"]
DatabaseScope = Literal["user", "org"]
DatabaseAccessMode = Literal["read_only", "read_write", "owner"]
_DATABASE_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_DATABASE_ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")


class AgentMemory(BaseModel):
    """Long-term memory declaration for composable/meta agents."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tiers: tuple[MemoryTier, ...] = ()
    namespace: str | None = None
    scope: Literal["agent", "user", "thread"] = "agent"
    retention: Literal["ephemeral", "durable"] = "durable"

    @field_validator("tiers", mode="before")
    @classmethod
    def _clean_tiers(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            raw_items = [value]
        elif isinstance(value, (list, tuple, set)):
            raw_items = list(value)
        else:
            raise ValueError("tiers must be a string or list of strings")
        seen: set[str] = set()
        out: list[str] = []
        for item in raw_items:
            tier = str(item or "").strip().lower()
            if not tier or tier in seen:
                continue
            seen.add(tier)
            out.append(tier)
        return tuple(out)

    @field_validator("namespace")
    @classmethod
    def _clean_namespace(cls, value: str | None) -> str | None:
        cleaned = str(value or "").strip()
        return cleaned or None

    def public_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tiers": list(self.tiers),
            "scope": self.scope,
            "retention": self.retention,
        }
        if self.namespace:
            payload["namespace"] = self.namespace
        return payload


class AgentDatabaseEnv(BaseModel):
    """Environment variables populated with platform-managed DB credentials."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str = "DATABASE_URL"

    @field_validator("url")
    @classmethod
    def _clean_env_name(cls, value: str) -> str:
        cleaned = (str(value or "").strip() or "DATABASE_URL").upper()
        if not _DATABASE_ENV_RE.fullmatch(cleaned):
            raise ValueError("database env.url must be an environment variable name")
        return cleaned

    def public_payload(self) -> dict[str, Any]:
        return {"url": self.url}


class AgentDatabaseMigrations(BaseModel):
    """Optional migrations path for a platform-managed database."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str | None = None

    @field_validator("path")
    @classmethod
    def _clean_path(cls, value: str | None) -> str | None:
        cleaned = str(value or "").strip()
        if not cleaned:
            return None
        if cleaned.startswith("/") or ".." in cleaned.split("/"):
            raise ValueError("database migrations.path must be a safe relative path")
        return cleaned

    def public_payload(self) -> dict[str, Any]:
        return {"path": self.path} if self.path else {}


class AgentDatabase(BaseModel):
    """Platform-managed Neon/Postgres database declaration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    engine: DatabaseEngine = "postgres"
    provider: Literal["neon"] = "neon"
    scope: DatabaseScope = "user"
    branch: str = "main"
    access_mode: DatabaseAccessMode = "read_write"
    env: AgentDatabaseEnv = Field(default_factory=AgentDatabaseEnv)
    migrations: AgentDatabaseMigrations | None = None
    scale_to_zero: bool = True

    @field_validator("name", "branch")
    @classmethod
    def _clean_slug(cls, value: str) -> str:
        cleaned = str(value or "").strip().lower()
        if not _DATABASE_SLUG_RE.fullmatch(cleaned):
            raise ValueError("database name/branch must be a slug")
        return cleaned

    @model_validator(mode="before")
    @classmethod
    def _aliases(cls, value: Any) -> Any:
        if isinstance(value, dict):
            raw = dict(value)
            if "access_mode" not in raw and "role" in raw:
                raw["access_mode"] = raw.pop("role")
            if "migrations" not in raw and raw.get("migrations_path") is not None:
                raw["migrations"] = {"path": raw.pop("migrations_path")}
            return raw
        return value

    def public_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "engine": self.engine,
            "provider": self.provider,
            "scope": self.scope,
            "branch": self.branch,
            "access_mode": self.access_mode,
            "env": self.env.public_payload(),
            "scale_to_zero": self.scale_to_zero,
        }
        if self.migrations is not None:
            migrations = self.migrations.public_payload()
            if migrations:
                payload["migrations"] = migrations
        return payload


class AgentPlatformResources(BaseModel):
    """Platform-managed resources declared in ``a2a.yaml``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    memory: AgentMemory | None = None
    databases: tuple[AgentDatabase, ...] = ()
    # Passthrough: the control plane provisions the mailbox; the SDK only
    # parses/echoes the declaration (``true`` or an options mapping).
    mailbox: bool | dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def _aliases(cls, value: Any) -> Any:
        if isinstance(value, dict):
            raw = dict(value)
            if isinstance(raw.get("databases"), dict):
                raw["databases"] = [raw["databases"]]
            if isinstance(raw.get("memory"), (str, list, tuple)):
                raw["memory"] = {"tiers": raw["memory"]}
            return raw
        return value

    @property
    def enabled(self) -> bool:
        return (
            self.memory is not None
            or bool(self.databases)
            or bool(self.mailbox)
        )

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "AgentPlatformResources":
        if not isinstance(data, Mapping):
            return cls()
        allowed = {
            key: value
            for key, value in data.items()
            if key in {"memory", "databases", "mailbox"}
        }
        return cls.model_validate(allowed) if allowed else cls()

    def public_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.memory is not None:
            payload["memory"] = self.memory.public_payload()
        if self.databases:
            payload["databases"] = [item.public_payload() for item in self.databases]
        if self.mailbox is not None:
            payload["mailbox"] = self.mailbox
        return payload


class SelfHealingPolicy(BaseModel):
    """Bounded, explicit source self-repair policy from ``a2a.yaml``.

    This is deliberately an opt-in deployment capability rather than an
    implicit runtime behavior.  The control plane applies the limits; the SDK
    validates and advertises the public, non-secret policy on the Agent Card.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    consecutive_failures: int = Field(default=1, ge=1, le=10)
    window_seconds: int = Field(default=300, ge=30, le=3600)
    cooldown_seconds: int = Field(default=900, ge=60, le=86400)
    max_repairs_per_day: int = Field(default=3, ge=1, le=20)
    max_turns: int = Field(default=30, ge=1, le=100)
    deployment_timeout_seconds: int = Field(default=1800, ge=60, le=7200)
    require_tests: bool = True

    @model_validator(mode="before")
    @classmethod
    def _concise_bool(cls, value: Any) -> Any:
        if isinstance(value, bool):
            return {"enabled": value}
        return value

    def public_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class MetaAgentManifest(BaseModel):
    """Typed meta-agent contract parsed from ``a2a.yaml`` or class attrs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    composition: AgentComposition | None = None
    goal: AgentGoal | None = None
    memory: AgentMemory | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.composition or self.goal or self.memory)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "MetaAgentManifest":
        raw = dict(data or {})
        composition = raw.get("composition")
        goal = raw.get("goal")
        memory = raw.get("memory")
        return cls(
            composition=AgentComposition.model_validate(composition)
            if isinstance(composition, (dict, list, tuple))
            else None,
            goal=AgentGoal.model_validate({"objective": goal})
            if isinstance(goal, str)
            else AgentGoal.model_validate(goal)
            if isinstance(goal, dict)
            else None,
            memory=AgentMemory.model_validate({"tiers": memory})
            if isinstance(memory, (str, list, tuple))
            else AgentMemory.model_validate(memory)
            if isinstance(memory, dict)
            else None,
        )

    def public_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"schema_version": "2026-06-02"}
        if self.composition is not None:
            payload["composition"] = self.composition.public_payload()
        if self.goal is not None:
            goal = self.goal.public_payload()
            if goal:
                payload["goal"] = goal
        if self.memory is not None:
            payload["memory"] = self.memory.public_payload()
        return payload if len(payload) > 1 else {}


def apply_project_manifest(
    agent_cls: type[Any],
    config: Mapping[str, Any] | None,
) -> MetaAgentManifest:
    """Attach parsed ``a2a.yaml`` platform metadata to an agent class.

    ``a2a run`` receives only an entrypoint, but the working directory still
    contains ``a2a.yaml``. Applying parsed metadata here makes local and
    deployed Agent Cards reflect declarative composition/lineage without
    importing control-plane code.
    """

    raw = dict(config or {})
    for attr in ("name", "version", "description"):
        value = raw.get(attr)
        if value is not None:
            cleaned = str(value).strip()
            if cleaned:
                setattr(agent_cls, attr, cleaned)
    manifest = MetaAgentManifest.from_mapping(config)
    if manifest.enabled:
        setattr(agent_cls, "meta_agent_manifest", manifest)
    lineage = TemplateLineage.from_mapping(
        raw.get("template_lineage") or raw.get("template")
    )
    if lineage.enabled:
        setattr(agent_cls, "template_lineage", lineage)
    platform_resources = AgentPlatformResources.from_mapping(raw.get("resources"))
    if platform_resources.enabled:
        setattr(agent_cls, "platform_resources", platform_resources)
    if "self_healing" in raw:
        self_healing = SelfHealingPolicy.model_validate(raw.get("self_healing"))
        setattr(agent_cls, "self_healing_policy", self_healing)
        capabilities = dict(getattr(agent_cls, "capabilities", {}) or {})
        capabilities["self_healing"] = self_healing.public_payload()
        setattr(agent_cls, "capabilities", capabilities)
    runtime = raw.get("runtime")
    if isinstance(runtime, Mapping):
        _apply_runtime_manifest(agent_cls, runtime)
    return manifest


def _apply_runtime_manifest(agent_cls: type[Any], runtime: Mapping[str, Any]) -> None:
    scalar_fields = {
        "lifecycle": Lifecycle,
        "availability": RuntimeAvailability,
        "state": State,
        "llm_provisioning": LLMProvisioning,
    }
    for field_name, enum_cls in scalar_fields.items():
        if runtime.get(field_name) is not None:
            setattr(agent_cls, field_name, enum_cls(str(runtime.get(field_name)).strip()))
    if runtime.get("resources") is not None:
        setattr(agent_cls, "resources", Resources.model_validate(runtime["resources"]))
    if runtime.get("concurrency") is not None:
        setattr(agent_cls, "concurrency", int(runtime["concurrency"]))
    if runtime.get("egress") is not None:
        setattr(agent_cls, "egress", EgressPolicy.model_validate(runtime["egress"]))
    if runtime.get("tools_used") is not None:
        value = runtime["tools_used"]
        raw_items = [value] if isinstance(value, str) else list(value or ())
        setattr(
            agent_cls,
            "tools_used",
            tuple(str(item).strip() for item in raw_items if str(item or "").strip()),
        )
    if runtime.get("pricing") is not None:
        # Legacy monetization block; no longer part of the runtime contract.
        warnings.warn(
            "a2a.yaml runtime.pricing is no longer supported and was ignored",
            stacklevel=2,
        )
    if runtime.get("account_access") is not None:
        setattr(
            agent_cls,
            "account_access",
            AccountAccess.model_validate(runtime["account_access"]),
        )
    if runtime.get("wants_cp_jwt") is not None:
        setattr(agent_cls, "wants_cp_jwt", _runtime_bool(runtime["wants_cp_jwt"]))
    endpoints = runtime.get("endpoints")
    if endpoints is None:
        endpoints = runtime.get("webhooks")
    if endpoints is not None:
        raw_items = [endpoints] if isinstance(endpoints, Mapping) else list(endpoints or ())
        setattr(
            agent_cls,
            "endpoints",
            tuple(AgentEndpoint.model_validate(item) for item in raw_items),
        )


def _runtime_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    cleaned = str(value or "").strip().lower()
    if cleaned in {"1", "true", "yes", "on"}:
        return True
    if cleaned in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid runtime boolean: {value!r}")


class AgentRuntime(BaseModel):
    """Aggregate runtime declaration; published on the Agent Card."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="before")
    @classmethod
    def _aliases(cls, value: Any) -> Any:
        if isinstance(value, dict):
            raw = dict(value)
            if "endpoints" not in raw and "webhooks" in raw:
                raw["endpoints"] = raw.pop("webhooks")
            # Tolerate legacy pricing blocks from older cards/manifests.
            raw.pop("pricing", None)
            return raw
        return value

    lifecycle: Lifecycle = Lifecycle.EPHEMERAL
    availability: RuntimeAvailability = RuntimeAvailability.ON_DEMAND
    state: State = State.NONE
    sandbox: Sandbox = Sandbox.MICROSANDBOX
    resources: Resources = Field(default_factory=Resources)
    concurrency: PositiveInt = 1
    egress: EgressPolicy = Field(default_factory=EgressPolicy)
    tools_used: tuple[str, ...] = ()
    llm_provisioning: LLMProvisioning = LLMProvisioning.PLATFORM
    account_access: AccountAccess = Field(default_factory=AccountAccess)
    wants_cp_jwt: bool = False
    """When True, the platform forwards the *caller's* control-plane JWT
    to this agent in the /invoke body so the skill can call back into
    user-scoped CP endpoints (/v1/me/files, /v1/agents/from-tarball,
    etc.) on the user's behalf. Only opt in for trusted platform agents
    — a JWT lets the holder do anything the user can do."""
    platform_resources: AgentPlatformResources = Field(
        default_factory=AgentPlatformResources
    )
    endpoints: tuple[AgentEndpoint, ...] = ()

    apt_packages: tuple[str, ...] = ()
    """Extra Debian packages to apt-install on top of the base image at
    build time. Use this when the agent needs system binaries the
    Python-only base doesn't ship — ffmpeg, imagemagick, poppler-utils,
    etc. Each entry must match ``[a-z0-9.+-]{2,64}`` (validated by the
    control plane); anything fancier (custom apt repos, --no-install-
    recommends overrides) belongs in a bespoke base image, not here."""

    @model_validator(mode="after")
    def _account_trial_requires_platform_llm(self) -> "AgentRuntime":
        if (
            self.account_access.platform_skill_calls
            and self.llm_provisioning
            not in {
                LLMProvisioning.PLATFORM,
                LLMProvisioning.PLATFORM_OR_CALLER_PROVIDED,
            }
        ):
            raise ValueError(
                "account_access.platform_skill_calls requires platform-capable "
                "llm_provisioning"
            )
        return self
