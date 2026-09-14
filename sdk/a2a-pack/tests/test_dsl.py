from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from a2a_pack import (
    AccountAccess,
    A2AAgent,
    APIKeyAuth,
    AgentComposition,
    AgentDatabase,
    AgentDsl,
    AgentDslAuth,
    AgentDslEntrypoint,
    AgentDslSkill,
    AgentEndpoint,
    AgentMemory,
    AgentPlatformResources,
    ConsumerSetup,
    ConsumerSetupField,
    EgressPolicy,
    JWTAuth,
    Lifecycle,
    LLMProvisioning,
    MetaAgentManifest,
    NoAuth,
    PlatformUserAuth,
    Resources,
    RunContext,
    State,
    StaticAuthResolver,
    TemplateLineage,
    WorkspaceAccess,
    WorkspaceMode,
    compile_agent_to_dsl,
    skill,
)


class PrimitiveInput(BaseModel):
    title: str
    count: int = 1
    enabled: bool = True


class PrimitiveOutput(BaseModel):
    ok: bool
    total: int


class PrimitiveConfig(BaseModel):
    region: str = "us"
    retries: int = 3


class PrimitiveAgent(A2AAgent[PrimitiveConfig, NoAuth]):
    name = "primitive-agent"
    description = "Exercises schema primitives"
    version = "1.2.3"
    config_model = PrimitiveConfig
    auth_model = NoAuth
    required_env = ("REGION",)
    required_secrets = ("API_TOKEN",)
    capabilities = {"custom": {"enabled": True}}
    input_modes = ("application/json", "text/plain")
    output_modes = ("application/json",)
    lifecycle = Lifecycle.WARM
    state = State.NONE
    resources = Resources(cpu="750m", memory="1Gi", max_runtime_seconds=1200)
    concurrency = 4
    egress = EgressPolicy(allow_hosts=("api.example.com",), deny_internet_by_default=False)
    workspace_access = WorkspaceAccess.dynamic(
        max_files=8,
        allowed_modes=(WorkspaceMode.READ_ONLY,),
        require_reason=True,
    )
    template_lineage = TemplateLineage(
        template_ref="starter/research",
        template_version="2",
        update_policy="notify",
    )
    meta_agent_manifest = MetaAgentManifest(
        composition=AgentComposition(
            sub_agents=[{"name": "writer", "skills": ["draft"]}],
            max_nodes=3,
        )
    )

    @skill(
        description="Exercise scalar parameters",
        tags=("primitives", "json-schema"),
        scopes=("read:things",),
        stream=True,
        timeout_seconds=12.5,
        idempotent=True,
        max_retries=2,
        cost_class="cheap",
        allow_scope_expansion=True,
        grant_mode="read_write_overlay",
        grant_allow_patterns=("{path}",),
        grant_deny_patterns=("private/**",),
        grant_outputs_prefix="outputs/",
        grant_write_prefixes=("outputs/",),
        grant_ttl_seconds=90,
        grant_run_timeout_seconds=80,
        grant_approval_timeout_seconds=10,
        grant_scope_approval_timeout_seconds=20,
    )
    async def scalars(
        self,
        ctx: RunContext[NoAuth],
        text: str,
        count: int,
        ratio: float,
        enabled: bool,
        maybe: str | None = None,
    ) -> str:
        return text

    @skill(description="Exercise container parameters and output")
    async def containers(
        self,
        ctx: RunContext[NoAuth],
        labels: list[str],
        weights: dict[str, int],
        payload: PrimitiveInput,
    ) -> PrimitiveOutput:
        return PrimitiveOutput(ok=True, total=len(labels) + sum(weights.values()))

    @skill(description="Exercise unstructured JSON")
    async def unstructured(
        self,
        ctx: RunContext[NoAuth],
        value: dict[str, Any],
    ) -> dict[str, Any]:
        return value


class ConsumerAuthAgent(A2AAgent[PrimitiveConfig, APIKeyAuth]):
    name = "consumer-auth-agent"
    description = "Exercises consumer setup and API-key auth"
    config_model = PrimitiveConfig
    auth_model = APIKeyAuth
    required_env = ("SERVICE_REGION",)
    required_secrets = ("SERVICE_TOKEN",)
    consumer_setup = ConsumerSetup.from_fields(
        ConsumerSetupField.secret(
            "GITHUB_TOKEN",
            label="GitHub token",
            description="Caller token used to read repositories.",
        ),
        ConsumerSetupField.config(
            "DEFAULT_REPO",
            label="Default repo",
            description="Fallback owner/repo.",
            required=False,
            input_type="text",
        ),
        ConsumerSetupField.config(
            "ENABLE_REVIEW",
            label="Enable review",
            required=False,
            input_type="boolean",
        ),
        ConsumerSetupField.config(
            "PLAN",
            label="Plan",
            input_type="select",
            options=("free", "pro"),
        ),
    )

    @skill(description="Needs caller setup")
    async def run(self, ctx: RunContext[APIKeyAuth], repo: str) -> str:
        return repo


class PlatformAuthAgent(A2AAgent[PrimitiveConfig, PlatformUserAuth]):
    name = "platform-auth-agent"
    description = "Requires platform session"
    config_model = PrimitiveConfig
    auth_model = PlatformUserAuth

    @skill(description="Return user")
    async def whoami(self, ctx: RunContext[PlatformUserAuth]) -> str:
        return ctx.auth.sub


class JwtAuthAgent(A2AAgent[PrimitiveConfig, JWTAuth]):
    name = "jwt-auth-agent"
    description = "Requires jwt auth"
    config_model = PrimitiveConfig
    auth_model = JWTAuth
    auth_resolver = StaticAuthResolver(JWTAuth(sub="fixture", scopes=["read"]))

    @skill(description="Return subject")
    async def subject(self, ctx: RunContext[JWTAuth]) -> str:
        return ctx.auth.sub


class CustomPrincipal(BaseModel):
    tenant: str
    subject: str
    scopes: list[str] = []


class CustomAuthAgent(A2AAgent[PrimitiveConfig, CustomPrincipal]):
    name = "custom-auth-agent"
    description = "Requires custom auth"
    config_model = PrimitiveConfig
    auth_model = CustomPrincipal

    @skill(description="Return tenant")
    async def tenant(self, ctx: RunContext[CustomPrincipal]) -> str:
        return ctx.auth.tenant


class SessionState(BaseModel):
    turns: int = 0
    last_subject: str | None = None


class FullRuntimeAgent(A2AAgent[PrimitiveConfig, PlatformUserAuth]):
    name = "full-runtime-agent"
    description = "Exercises every runtime aggregate field"
    config_model = PrimitiveConfig
    auth_model = PlatformUserAuth
    lifecycle = Lifecycle.SESSION
    state = State.SESSION
    state_model = SessionState
    resources = Resources(cpu="2", memory="2Gi", gpu=1, max_runtime_seconds=1800)
    concurrency = 12
    egress = EgressPolicy(
        allow_hosts=("api.example.com", "storage.example.com"),
        allow_internal_services=("control-plane",),
        deny_internet_by_default=True,
    )
    tools_used = ("a2a", "sandbox", "llm")
    llm_provisioning = LLMProvisioning.PLATFORM_OR_CALLER_PROVIDED
    account_access = AccountAccess(required=True, platform_skill_calls=5)
    endpoints = (
        AgentEndpoint(
            name="telegram",
            path="/telegram/webhook",
            methods=("POST",),
            skill="run",
            body_arg="update",
            headers_arg="headers",
            query_arg="query",
        ),
    )
    platform_resources = AgentPlatformResources(
        memory=AgentMemory(tiers=("kv", "vector"), namespace="full-runtime"),
        databases=(
            AgentDatabase(
                name="app",
                scope="user",
                branch="main",
                env={"url": "APP_DATABASE_URL"},
                migrations={"path": "db/migrations"},
            ),
        ),
    )
    wants_cp_jwt = True

    @skill(description="Run with state")
    async def run(self, ctx: RunContext[PlatformUserAuth]) -> dict[str, str]:
        return {"subject": ctx.auth.sub}


def _compile() -> AgentDsl:
    return compile_agent_to_dsl(
        PrimitiveAgent,
        entrypoint="agents.primitive:PrimitiveAgent",
        metadata={"compiler": "pytest"},
    )


def _skill(dsl: AgentDsl, name: str) -> AgentDslSkill:
    return next(skill for skill in dsl.skills if skill.name == name)


def _public_auth() -> AgentDslAuth:
    return AgentDslAuth(
        model="a2a_pack.auth.NoAuth",
        strategy="public",
        principal_schema=NoAuth.model_json_schema(),
        required=False,
    )


def test_dsl_has_mapping_for_all_a2aagent_declarative_fields() -> None:
    """Guard the sidecar contract when new A2AAgent class attrs are added."""

    coverage = {
        "name": "AgentDsl.name",
        "description": "AgentDsl.description",
        "version": "AgentDsl.version",
        "config_model": "AgentDsl.config_schema",
        "auth_model": "AgentDsl.auth.principal_schema",
        "auth_resolver": "AgentDsl.auth.resolver",
        "required_secrets": "AgentDsl.required_secrets",
        "required_env": "AgentDsl.required_env",
        "consumer_setup": "AgentDsl.consumer_setup",
        "capabilities": "AgentDsl.capabilities",
        "meta_agent_manifest": "AgentDsl.meta_agent_manifest",
        "template_lineage": "AgentDsl.template_lineage",
        "input_modes": "AgentDsl.input_modes",
        "output_modes": "AgentDsl.output_modes",
        "lifecycle": "AgentDsl.runtime.lifecycle",
        "availability": "AgentDsl.runtime.availability",
        "state": "AgentDsl.runtime.state",
        "state_model": "AgentDsl.state_schema",
        "resources": "AgentDsl.runtime.resources",
        "concurrency": "AgentDsl.runtime.concurrency",
        "egress": "AgentDsl.runtime.egress",
        "tools_used": "AgentDsl.runtime.tools_used",
        "workspace_access": "AgentDsl.workspace_access",
        "llm_provisioning": "AgentDsl.runtime.llm_provisioning",
        "account_access": "AgentDsl.runtime.account_access",
        "platform_resources": "AgentDsl.runtime.platform_resources",
        "wants_cp_jwt": "AgentDsl.runtime.wants_cp_jwt",
        "endpoints": "AgentDsl.runtime.endpoints",
        "_skills": "AgentDsl.skills",
    }
    declared = set(A2AAgent.__annotations__)

    assert declared == set(coverage)


def test_dsl_compiles_primitive_input_and_output_schemas() -> None:
    dsl = _compile()
    scalars = _skill(dsl, "scalars")
    props = scalars.input_schema["properties"]

    assert props["text"]["type"] == "string"
    assert props["count"]["type"] == "integer"
    assert props["ratio"]["type"] == "number"
    assert props["enabled"]["type"] == "boolean"
    assert {"type": "null"} in props["maybe"]["anyOf"]
    assert scalars.input_schema["required"] == ["text", "count", "ratio", "enabled"]
    assert scalars.input_schema["additionalProperties"] is False
    assert scalars.output_schema["type"] == "string"


def test_skill_input_schema_is_real_request_shape() -> None:
    dsl = _compile()
    for compiled_skill in dsl.skills:
        schema = compiled_skill.input_schema
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict)
        assert isinstance(schema["required"], list)
        assert schema["additionalProperties"] is False
        assert set(schema["required"]).issubset(schema["properties"])


def test_dsl_compiles_container_and_model_schemas() -> None:
    containers = _skill(_compile(), "containers")
    props = containers.input_schema["properties"]

    assert props["labels"]["type"] == "array"
    assert props["labels"]["items"]["type"] == "string"
    assert props["weights"]["type"] == "object"
    assert props["weights"]["additionalProperties"]["type"] == "integer"
    assert props["payload"]["type"] == "object"
    assert props["payload"]["properties"]["title"]["type"] == "string"
    assert props["payload"]["properties"]["count"]["type"] == "integer"
    assert props["payload"]["properties"]["enabled"]["type"] == "boolean"
    assert props["payload"]["required"] == ["title"]
    assert containers.output_schema["type"] == "object"
    assert containers.output_schema["properties"]["ok"]["type"] == "boolean"
    assert containers.output_schema["properties"]["total"]["type"] == "integer"
    assert containers.output_schema["required"] == ["ok", "total"]


def test_dsl_compiles_unstructured_json_schema() -> None:
    unstructured = _skill(_compile(), "unstructured")

    assert unstructured.input_schema["properties"]["value"]["type"] == "object"
    assert unstructured.output_schema["type"] == "object"


def test_dsl_preserves_runtime_workspace_and_lineage_metadata() -> None:
    dsl = _compile()

    assert dsl.name == "primitive-agent"
    assert dsl.description == "Exercises schema primitives"
    assert dsl.version == "1.2.3"
    assert dsl.language == "python"
    assert dsl.entrypoint.module == "agents.primitive"
    assert dsl.entrypoint.class_name == "PrimitiveAgent"
    assert dsl.required_env == ("REGION",)
    assert dsl.required_secrets == ("API_TOKEN",)
    assert dsl.input_modes == ("application/json", "text/plain")
    assert dsl.output_modes == ("application/json",)
    assert dsl.capabilities == {"custom": {"enabled": True}}
    assert dsl.runtime.lifecycle == Lifecycle.WARM
    assert dsl.runtime.resources.cpu == "750m"
    assert dsl.runtime.resources.memory == "1Gi"
    assert dsl.runtime.resources.max_runtime_seconds == 1200
    assert dsl.runtime.concurrency == 4
    assert dsl.runtime.egress.allow_hosts == ("api.example.com",)
    assert dsl.runtime.egress.deny_internet_by_default is False
    assert dsl.workspace_access.enabled is True
    assert dsl.workspace_access.max_files == 8
    assert dsl.workspace_access.allowed_modes == (WorkspaceMode.READ_ONLY,)
    assert dsl.workspace_access.require_reason is True
    assert dsl.template_lineage is not None
    assert dsl.template_lineage.template_ref == "starter/research"
    assert dsl.meta_agent_manifest is not None
    assert dsl.meta_agent_manifest.composition is not None
    assert dsl.meta_agent_manifest.composition.sub_agents[0].name == "writer"
    assert dsl.config_schema["properties"]["region"]["default"] == "us"
    assert dsl.auth.model == "a2a_pack.auth.NoAuth"
    assert dsl.auth.strategy == "public"
    assert dsl.auth.required is False
    assert dsl.auth.resolver is None
    assert dsl.auth.principal_schema["title"] == "NoAuth"
    assert dsl.metadata == {"compiler": "pytest"}


def test_dsl_preserves_all_runtime_aggregate_fields() -> None:
    dsl = compile_agent_to_dsl(FullRuntimeAgent, entrypoint="agent:FullRuntimeAgent")

    assert dsl.runtime.lifecycle == Lifecycle.SESSION
    assert dsl.runtime.availability.value == "on_demand"
    assert dsl.runtime.state == State.SESSION
    assert dsl.state_schema is not None
    assert dsl.state_schema["properties"]["turns"]["default"] == 0
    assert dsl.state_schema["properties"]["last_subject"]["anyOf"][0]["type"] == "string"
    assert dsl.runtime.resources.cpu == "2"
    assert dsl.runtime.resources.memory == "2Gi"
    assert dsl.runtime.resources.gpu == 1
    assert dsl.runtime.resources.max_runtime_seconds == 1800
    assert dsl.runtime.concurrency == 12
    assert dsl.runtime.egress.allow_hosts == ("api.example.com", "storage.example.com")
    assert dsl.runtime.egress.allow_internal_services == ("control-plane",)
    assert dsl.runtime.egress.deny_internet_by_default is True
    assert dsl.runtime.tools_used == ("a2a", "sandbox", "llm")
    assert dsl.runtime.llm_provisioning == LLMProvisioning.PLATFORM_OR_CALLER_PROVIDED
    assert dsl.runtime.account_access.required is True
    assert dsl.runtime.account_access.platform_skill_calls == 5
    assert dsl.runtime.account_access.after_trial == "byok"
    assert len(dsl.runtime.endpoints) == 1
    endpoint = dsl.runtime.endpoints[0]
    assert endpoint.name == "telegram"
    assert endpoint.path == "/telegram/webhook"
    assert endpoint.methods == ("POST",)
    assert endpoint.skill == "run"
    assert endpoint.body_arg == "update"
    assert endpoint.headers_arg == "headers"
    assert endpoint.query_arg == "query"
    assert dsl.runtime.platform_resources.memory is not None
    assert dsl.runtime.platform_resources.memory.tiers == ("kv", "vector")
    assert dsl.runtime.platform_resources.memory.namespace == "full-runtime"
    assert len(dsl.runtime.platform_resources.databases) == 1
    database = dsl.runtime.platform_resources.databases[0]
    assert database.name == "app"
    assert database.provider == "neon"
    assert database.env.url == "APP_DATABASE_URL"
    assert database.migrations is not None
    assert database.migrations.path == "db/migrations"
    assert dsl.runtime.wants_cp_jwt is True

    card = dsl.to_agent_card()
    assert card.runtime.state == State.SESSION
    assert card.state_schema == dsl.state_schema
    assert card.runtime.tools_used == ("a2a", "sandbox", "llm")
    assert card.runtime.platform_resources.public_payload() == {
        "memory": {
            "tiers": ["kv", "vector"],
            "namespace": "full-runtime",
            "scope": "agent",
            "retention": "durable",
        },
        "databases": [
            {
                "name": "app",
                "engine": "postgres",
                "provider": "neon",
                "scope": "user",
                "branch": "main",
                "access_mode": "read_write",
                "env": {"url": "APP_DATABASE_URL"},
                "scale_to_zero": True,
                "migrations": {"path": "db/migrations"},
            }
        ],
    }
    assert card.runtime.wants_cp_jwt is True


def test_dsl_emits_consumer_setup_required_env_and_secrets() -> None:
    dsl = compile_agent_to_dsl(ConsumerAuthAgent, entrypoint="agent:ConsumerAuthAgent")

    assert dsl.required_env == ("SERVICE_REGION",)
    assert dsl.required_secrets == ("SERVICE_TOKEN",)
    assert dsl.consumer_setup.required_names == ("GITHUB_TOKEN", "PLAN")
    fields = {field.name: field for field in dsl.consumer_setup.fields}
    assert fields["GITHUB_TOKEN"].kind == "secret"
    assert fields["GITHUB_TOKEN"].input_type == "password"
    assert fields["GITHUB_TOKEN"].label == "GitHub token"
    assert fields["GITHUB_TOKEN"].description == "Caller token used to read repositories."
    assert fields["DEFAULT_REPO"].kind == "config"
    assert fields["DEFAULT_REPO"].required is False
    assert fields["ENABLE_REVIEW"].input_type == "boolean"
    assert fields["ENABLE_REVIEW"].required is False
    assert fields["PLAN"].input_type == "select"
    assert fields["PLAN"].options == ("free", "pro")

    dumped = dsl.model_dump(mode="json")
    dumped_fields = {field["name"]: field for field in dumped["consumer_setup"]["fields"]}
    assert dumped_fields["GITHUB_TOKEN"]["kind"] == "secret"
    assert dumped_fields["PLAN"]["options"] == ["free", "pro"]

    card = dsl.to_agent_card()
    assert card.required_env == ["SERVICE_REGION"]
    assert card.required_secrets == ["SERVICE_TOKEN"]
    assert card.consumer_setup.required_names == ("GITHUB_TOKEN", "PLAN")


def test_dsl_emits_api_key_auth_contract() -> None:
    dsl = compile_agent_to_dsl(ConsumerAuthAgent, entrypoint="agent:ConsumerAuthAgent")

    assert dsl.auth.model == "a2a_pack.auth.APIKeyAuth"
    assert dsl.auth.strategy == "api_key"
    assert dsl.auth.required is True
    assert dsl.auth.resolver is None
    assert dsl.auth.principal_schema["required"] == ["api_key_id"]
    assert dsl.auth.principal_schema["properties"]["api_key_id"]["type"] == "string"
    assert dsl.auth.principal_schema["properties"]["scopes"]["type"] == "array"


def test_dsl_emits_platform_user_auth_contract() -> None:
    dsl = compile_agent_to_dsl(PlatformAuthAgent, entrypoint="agent:PlatformAuthAgent")

    assert dsl.auth.model == "a2a_pack.auth.PlatformUserAuth"
    assert dsl.auth.strategy == "platform_user"
    assert dsl.auth.required is True
    assert dsl.auth.principal_schema["required"] == ["sub"]
    assert dsl.auth.principal_schema["properties"]["sub"]["type"] == "string"
    assert dsl.auth.principal_schema["properties"]["user_id"]["anyOf"][0]["type"] == "integer"
    assert dsl.auth.principal_schema["properties"]["org_slug"]["anyOf"][0]["type"] == "string"


def test_dsl_emits_jwt_auth_and_resolver_contract() -> None:
    dsl = compile_agent_to_dsl(JwtAuthAgent, entrypoint="agent:JwtAuthAgent")

    assert dsl.auth.model == "a2a_pack.auth.JWTAuth"
    assert dsl.auth.strategy == "jwt"
    assert dsl.auth.required is True
    assert dsl.auth.resolver == "a2a_pack.auth.StaticAuthResolver"
    assert dsl.auth.principal_schema["required"] == ["sub"]
    assert dsl.auth.principal_schema["properties"]["sub"]["type"] == "string"
    assert dsl.auth.principal_schema["properties"]["email"]["anyOf"][0]["type"] == "string"
    assert dsl.auth.principal_schema["properties"]["scopes"]["items"]["type"] == "string"


def test_dsl_emits_custom_auth_model_contract() -> None:
    dsl = compile_agent_to_dsl(CustomAuthAgent, entrypoint="agent:CustomAuthAgent")

    assert dsl.auth.model.endswith("CustomPrincipal")
    assert dsl.auth.strategy == "custom"
    assert dsl.auth.required is True
    assert dsl.auth.principal_schema["required"] == ["tenant", "subject"]
    assert dsl.auth.principal_schema["properties"]["tenant"]["type"] == "string"
    assert dsl.auth.principal_schema["properties"]["subject"]["type"] == "string"
    assert dsl.auth.principal_schema["properties"]["scopes"]["type"] == "array"


def test_dsl_preserves_stream_policy_and_grant_hints() -> None:
    scalars = _skill(_compile(), "scalars")

    assert scalars.handler == "scalars"
    assert scalars.tags == ("primitives", "json-schema")
    assert scalars.scopes == ("read:things",)
    assert scalars.stream is True
    assert scalars.policy.timeout_seconds == 12.5
    assert scalars.policy.idempotent is True
    assert scalars.policy.max_retries == 2
    assert scalars.policy.cost_class == "cheap"
    assert scalars.policy.allow_scope_expansion is True
    assert scalars.policy.grant_mode == "read_write_overlay"
    assert scalars.policy.grant_allow_patterns == ("{path}",)
    assert scalars.policy.grant_deny_patterns == ("private/**",)
    assert scalars.policy.grant_outputs_prefix == "outputs/"
    assert scalars.policy.grant_write_prefixes == ("outputs/",)
    assert scalars.policy.grant_ttl_seconds == 90
    assert scalars.policy.grant_run_timeout_seconds == 80
    assert scalars.policy.grant_approval_timeout_seconds == 10
    assert scalars.policy.grant_scope_approval_timeout_seconds == 20


def test_dsl_projects_to_agent_card_without_losing_public_metadata() -> None:
    card = _compile().to_agent_card()

    assert card.name == "primitive-agent"
    assert card.version == "1.2.3"
    assert [skill.name for skill in card.skills] == [
        "scalars",
        "containers",
        "unstructured",
    ]
    assert card.skills[0].stream is True
    assert card.skills[0].policy.grant_mode == "read_write_overlay"
    assert card.capabilities["custom"] == {"enabled": True}
    assert card.capabilities["a2a_pack"]["dsl_schema_version"] == "2026-06-04"
    assert card.capabilities["a2a_pack"]["language"] == "python"
    assert card.capabilities["meta_agent"]["composition"]["sub_agents"][0]["name"] == "writer"
    assert card.workspace_access.enabled is True
    assert card.runtime.lifecycle == Lifecycle.WARM
    assert card.mcp_endpoint == "/mcp"
    assert card.connector_mcp_endpoint == "/connector-mcp"


def test_dsl_round_trips_through_json_validation() -> None:
    dsl = _compile()

    parsed = AgentDsl.model_validate_json(dsl.model_dump_json())

    assert parsed == dsl
    assert parsed.skills[1].output_schema["properties"]["total"]["type"] == "integer"


def test_entrypoint_string_parsing_variants() -> None:
    module_only = compile_agent_to_dsl(PrimitiveAgent, entrypoint="agents.primitive")
    empty = compile_agent_to_dsl(PrimitiveAgent)
    command = AgentDslEntrypoint(command=("node", "dist/server.js"))
    with_command = compile_agent_to_dsl(PrimitiveAgent, entrypoint=command)

    assert module_only.entrypoint.module == "agents.primitive"
    assert module_only.entrypoint.class_name is None
    assert empty.entrypoint.module is None
    assert empty.entrypoint.class_name is None
    assert with_command.entrypoint.command == ("node", "dist/server.js")


def test_agent_dsl_requires_at_least_one_skill() -> None:
    with pytest.raises(ValidationError, match="at least one skill"):
        AgentDsl(
            language="typescript",
            name="empty",
            description="empty",
            version="0.1.0",
            entrypoint=AgentDslEntrypoint(command=("node", "dist/server.js")),
            auth=_public_auth(),
            skills=(),
        )


def test_agent_dsl_requires_explicit_auth_contract() -> None:
    with pytest.raises(ValidationError, match="auth"):
        AgentDsl(
            language="typescript",
            name="missing-auth",
            description="missing auth",
            version="0.1.0",
            entrypoint=AgentDslEntrypoint(command=("node", "dist/server.js")),
            skills=(
                AgentDslSkill(
                    name="run",
                    description="Run",
                    handler="run",
                    input_schema={"type": "object", "properties": {}, "required": []},
                    output_schema={"type": "string"},
                ),
            ),
        )


def test_agent_dsl_rejects_duplicate_skill_names() -> None:
    base = AgentDslSkill(
        name="run",
        description="Run",
        handler="run",
        input_schema={"type": "object", "properties": {}, "required": []},
        output_schema={"type": "string"},
    )

    with pytest.raises(ValidationError, match="duplicate skill names"):
        AgentDsl(
            language="go",
            name="dup",
            description="dup",
            version="0.1.0",
            entrypoint=AgentDslEntrypoint(command=("agent",)),
            auth=_public_auth(),
            skills=(base, base.model_copy()),
        )


def test_agent_dsl_skill_requires_name_and_handler() -> None:
    with pytest.raises(ValidationError, match="must not be empty"):
        AgentDslSkill(
            name="",
            description="bad",
            handler="run",
            input_schema={"type": "object", "properties": {}, "required": []},
            output_schema={"type": "string"},
        )

    with pytest.raises(ValidationError, match="must not be empty"):
        AgentDslSkill(
            name="run",
            description="bad",
            handler="",
            input_schema={"type": "object", "properties": {}, "required": []},
            output_schema={"type": "string"},
        )


def test_agent_dsl_skill_rejects_non_object_input_schema() -> None:
    with pytest.raises(ValidationError, match="JSON object schema"):
        AgentDslSkill(
            name="run",
            description="bad",
            handler="run",
            input_schema={"type": "string"},
            output_schema={"type": "string"},
        )


def test_agent_dsl_skill_requires_properties_and_required_list() -> None:
    with pytest.raises(ValidationError, match="define properties"):
        AgentDslSkill(
            name="run",
            description="bad",
            handler="run",
            input_schema={"type": "object", "required": []},
            output_schema={"type": "string"},
        )

    with pytest.raises(ValidationError, match="required as a string list"):
        AgentDslSkill(
            name="run",
            description="bad",
            handler="run",
            input_schema={"type": "object", "properties": {}, "required": "x"},
            output_schema={"type": "string"},
        )


def test_agent_dsl_skill_rejects_required_fields_outside_properties() -> None:
    with pytest.raises(ValidationError, match="required fields are not properties"):
        AgentDslSkill(
            name="run",
            description="bad",
            handler="run",
            input_schema={
                "type": "object",
                "properties": {"known": {"type": "string"}},
                "required": ["known", "missing"],
            },
            output_schema={"type": "string"},
        )


def test_agent_dsl_skill_requires_output_schema() -> None:
    with pytest.raises(ValidationError, match="output_schema must not be empty"):
        AgentDslSkill(
            name="run",
            description="bad",
            handler="run",
            input_schema={"type": "object", "properties": {}, "required": []},
            output_schema={},
        )
