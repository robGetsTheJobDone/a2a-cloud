from __future__ import annotations

import asyncio
from typing import Annotated

import pytest
from pydantic import BaseModel, Field

from a2a_pack import (
    AccountAccess,
    A2AAgent,
    AgentCard,
    APIKeyAuth,
    ConsumerSetup,
    ConsumerSetupField,
    ConsumerSetupMissing,
    AgentDsl,
    EgressPolicy,
    FileUpload,
    JWTAuth,
    Lifecycle,
    LocalRunContext,
    MissingScopes,
    NoAuth,
    Resources,
    RunContext,
    Sandbox,
    SkillInputError,
    SkillInvocationError,
    SkillNotFound,
    State,
    TemplateLineage,
    UploadedFile,
    compile_agent_to_dsl,
    skill,
)
from a2a_pack import __version__ as A2A_PACK_VERSION


class _GreeterConfig(BaseModel):
    suffix: str = "!"


class _Greeter(A2AAgent[_GreeterConfig, NoAuth]):
    name = "greeter"
    description = "Says hi"
    config_model = _GreeterConfig
    auth_model = NoAuth

    @skill(description="Greet someone")
    async def greet(self, ctx: RunContext[NoAuth], who: str, loud: bool = False) -> str:
        await ctx.emit_progress(f"greeting {who}")
        out = f"hello {who}{self.config.suffix}"
        return out.upper() if loud else out

    @skill(name="boom", description="Fails on purpose")
    async def _boom(self, ctx: RunContext[NoAuth]) -> str:
        raise ValueError("nope")


class _BoundsAgent(A2AAgent[_GreeterConfig, NoAuth]):
    name = "bounds-agent"
    description = "Validates Field constraints on skill parameters"
    config_model = _GreeterConfig
    auth_model = NoAuth

    @skill(description="Bounded wait")
    async def wait(
        self,
        ctx: RunContext[NoAuth],
        seconds: int = Field(default=2, ge=1, le=3),
    ) -> int:
        return seconds


def _ctx() -> LocalRunContext[NoAuth]:
    return LocalRunContext(auth=NoAuth())


# --- subclass / decorator validation ---

def test_subclass_without_name_rejected():
    with pytest.raises(TypeError, match="name must be set"):

        class _Bad(A2AAgent):
            description = "missing name"


def test_skill_requires_async():
    with pytest.raises(TypeError, match="async"):

        class _Sync(A2AAgent):
            name = "sync"

            @skill(description="sync handler")
            def hi(self, ctx: RunContext[NoAuth]) -> str:  # type: ignore[misc]
                return "hi"


def test_skill_requires_run_context_param():
    with pytest.raises(TypeError, match="RunContext"):

        class _NoCtx(A2AAgent):
            name = "noctx"

            @skill(description="missing ctx")
            async def hi(self, who: str) -> str:
                return who


def test_skill_rejects_var_args():
    with pytest.raises(TypeError, match=r"\*args"):

        class _Va(A2AAgent):
            name = "va"

            @skill()
            async def hi(self, ctx: RunContext[NoAuth], *args: str) -> str:
                return ",".join(args)


def test_skill_rejects_var_kwargs():
    with pytest.raises(TypeError, match=r"\*\*kwargs"):

        class _Vk(A2AAgent):
            name = "vk"

            @skill()
            async def hi(self, ctx: RunContext[NoAuth], **kwargs: str) -> str:
                return str(kwargs)


def test_skill_rejects_untyped_param():
    with pytest.raises(TypeError, match="missing a type annotation"):

        class _U(A2AAgent):
            name = "u"

            @skill()
            async def hi(self, ctx: RunContext[NoAuth], who) -> str:  # type: ignore[no-untyped-def]
                return who


def test_skill_rejects_reserved_name():
    with pytest.raises(TypeError, match="reserved"):

        class _R(A2AAgent):
            name = "r"

            @skill()
            async def hi(self, ctx: RunContext[NoAuth], context: str) -> str:
                return context


def test_duplicate_skill_name_rejected():
    with pytest.raises(TypeError, match="duplicate skill name"):

        class _Dup(A2AAgent):
            name = "dup"

            @skill(name="x")
            async def a(self, ctx: RunContext[NoAuth]) -> str:
                return "a"

            @skill(name="x")
            async def b(self, ctx: RunContext[NoAuth]) -> str:
                return "b"


# --- card / metadata ---

def test_skills_collected_with_metadata():
    g = _Greeter()
    assert set(g.skills) == {"greet", "boom"}
    assert g.skills["greet"].description == "Greet someone"


def test_card_omits_ctx_param():
    card = _Greeter().card()
    assert isinstance(card, AgentCard)
    greet = next(s for s in card.skills if s.name == "greet")
    assert "ctx" not in greet.input_schema["properties"]
    assert greet.input_schema["required"] == ["who"]
    assert greet.input_schema["additionalProperties"] is False


def test_card_publishes_account_gated_platform_trial():
    class _TrialAgent(A2AAgent):
        name = "trial-agent"
        account_access = AccountAccess(required=True, platform_skill_calls=3)

        @skill()
        async def run(self, ctx: RunContext[NoAuth]) -> str:
            return "ok"

    access = _TrialAgent().card().runtime.account_access
    assert access.required is True
    assert access.platform_skill_calls == 3
    assert access.after_trial == "byok"


def test_account_trial_requires_account_gate():
    with pytest.raises(ValueError, match="account access must be required"):
        AccountAccess(platform_skill_calls=1)


def test_file_upload_marker_propagates_to_skill_schema():
    class _UploadAgent(A2AAgent[BaseModel, NoAuth]):
        name = "upload-agent"

        @skill(description="Analyze an uploaded PDF")
        async def analyze(
            self,
            ctx: RunContext[NoAuth],
            document: Annotated[
                UploadedFile,
                FileUpload(
                    accept=("application/pdf",),
                    max_bytes=10_000_000,
                    description="PDF to analyze",
                ),
            ],
            question: str,
        ) -> dict[str, str]:
            return {"path": document.path, "question": question}

    skill_card = _UploadAgent().card().skills[0]
    schema = skill_card.input_schema
    document = schema["properties"]["document"]

    assert schema["required"] == ["document", "question"]
    assert document["x-a2a-file-upload"] == {
        "required_upload": True,
        "accept": ["application/pdf"],
        "max_bytes": 10_000_000,
        "description": "PDF to analyze",
    }
    assert document["type"] == "object"
    assert set(document["properties"]) == {"path", "filename", "media_type", "size_bytes"}


def test_card_includes_deploy_metadata():
    class _Cfg(BaseModel):
        pass

    class _Meta(A2AAgent[_Cfg, NoAuth]):
        name = "meta"
        description = "metadata showcase"
        required_secrets = ("OPENAI_KEY",)
        required_env = ("REGION",)
        capabilities = {"streaming": True}
        input_modes = ("application/json", "text/plain")
        output_modes = ("application/json",)

        @skill()
        async def noop(self, ctx: RunContext[NoAuth]) -> str:
            return "ok"

    card = _Meta().card()
    assert card.required_secrets == ["OPENAI_KEY"]
    assert card.required_env == ["REGION"]
    assert card.capabilities["streaming"] is True
    assert card.input_modes == ["application/json", "text/plain"]


def test_python_agent_compiles_to_sidecar_dsl():
    dsl = compile_agent_to_dsl(_Greeter, entrypoint="agent:Greeter")

    assert isinstance(dsl, AgentDsl)
    assert dsl.schema_version == "2026-06-04"
    assert dsl.language == "python"
    assert dsl.name == "greeter"
    assert dsl.entrypoint.module == "agent"
    assert dsl.entrypoint.class_name == "Greeter"
    assert dsl.config_schema["properties"]["suffix"]["default"] == "!"
    assert dsl.auth.principal_schema["title"] == "NoAuth"

    greet = next(skill for skill in dsl.skills if skill.name == "greet")
    assert greet.handler == "greet"
    assert greet.input_schema["required"] == ["who"]
    assert greet.output_schema["type"] == "string"

    card = dsl.to_agent_card()
    assert card.name == "greeter"
    assert card.capabilities["a2a_pack"]["dsl_schema_version"] == "2026-06-04"
    assert card.capabilities["a2a_pack"]["language"] == "python"
    assert [skill.name for skill in card.skills] == ["greet", "boom"]


def test_card_includes_optional_consumer_setup():
    class _Cfg(BaseModel):
        pass

    class _SetupAgent(A2AAgent[_Cfg, NoAuth]):
        name = "needs-caller-setup"
        description = "needs caller setup"
        consumer_setup = ConsumerSetup.from_fields(
            ConsumerSetupField.secret(
                "GITHUB_TOKEN",
                label="GitHub token",
                description="Token used to read the caller's repositories.",
            ),
            ConsumerSetupField.config(
                "DEFAULT_REPO",
                label="Default repository",
                required=False,
            ),
        )

        @skill()
        async def noop(self, ctx: RunContext[NoAuth]) -> str:
            return "ok"

    card = _SetupAgent().card()

    assert [field.name for field in card.consumer_setup.fields] == [
        "GITHUB_TOKEN",
        "DEFAULT_REPO",
    ]
    assert card.consumer_setup.required_names == ("GITHUB_TOKEN",)


def test_card_advertises_a2a_pack_version_in_capabilities():
    card = _Greeter().card()
    assert card.capabilities["a2a_pack"] == {
        "package": "a2a-pack",
        "version": A2A_PACK_VERSION,
    }


def test_card_omits_template_lineage_until_agent_opts_in():
    payload = _Greeter().card().model_dump(mode="json")

    assert "template_lineage" not in payload


def test_card_includes_template_lineage_when_declared():
    class _Templated(A2AAgent):
        name = "smtp-email-sender"
        description = "SMTP email sender copied from a platform template."
        version = "0.1.0"
        template_lineage = TemplateLineage(
            template_ref="a2acloud/templates/smtp-email-agent",
            template_version="0.3.0",
            source_agent="smtp-email-template",
            source_agent_version="0.3.0",
            source_revision="abc123",
            instance_version="0.1.0",
            update_policy="propose",
            update_channel="stable",
            migration_skill="apply_template_update",
        )

        @skill()
        async def noop(self, ctx: RunContext[NoAuth]) -> str:
            return "ok"

    card = _Templated().card()
    payload = card.model_dump(mode="json")

    assert card.template_lineage is not None
    assert payload["template_lineage"] == {
        "schema_version": "2026-06-02",
        "template_ref": "a2acloud/templates/smtp-email-agent",
        "template_version": "0.3.0",
        "source_agent": "smtp-email-template",
        "source_agent_version": "0.3.0",
        "source_revision": "abc123",
        "instance_version": "0.1.0",
        "update_policy": "propose",
        "update_channel": "stable",
        "migration_skill": "apply_template_update",
    }


# --- config hydration ---

def test_default_config_constructed_when_none():
    g = _Greeter()
    assert g.config.suffix == "!"


def test_explicit_config_used():
    g = _Greeter(_GreeterConfig(suffix="?!"))
    assert g.config.suffix == "?!"


def test_config_accepts_dict():
    g = _Greeter({"suffix": "?"})
    assert g.config.suffix == "?"


def test_config_dict_validation_errors_propagate():
    with pytest.raises(Exception):
        _Greeter({"suffix": 123, "extra": True})  # type: ignore[arg-type]


# --- invocation ---

async def test_invoke_passes_ctx_and_returns_value():
    g = _Greeter()
    ctx = _ctx()
    assert await g.invoke("greet", ctx, who="bob") == "hello bob!"
    assert any(e.kind == "progress" for e in ctx.events)


async def test_invoke_validates_input_types():
    g = _Greeter()
    with pytest.raises(SkillInputError):
        await g.invoke("greet", _ctx(), who=123)  # type: ignore[arg-type]


async def test_invoke_rejects_unknown_param():
    g = _Greeter()
    with pytest.raises(SkillInputError, match="unknown parameter"):
        await g.invoke("greet", _ctx(), who="bob", extra="nope")


async def test_invoke_missing_required_param():
    g = _Greeter()
    with pytest.raises(SkillInputError, match="missing required"):
        await g.invoke("greet", _ctx())


async def test_invoke_enforces_field_constraints_and_defaults():
    agent = _BoundsAgent()

    schema = agent.skills["wait"].input_schema["properties"]["seconds"]
    assert schema["minimum"] == 1
    assert schema["maximum"] == 3

    assert await agent.invoke("wait", _ctx()) == 2
    assert await agent.invoke("wait", _ctx(), seconds="3") == 3
    with pytest.raises(SkillInputError):
        await agent.invoke("wait", _ctx(), seconds=4)


async def test_invoke_unknown_skill_raises():
    g = _Greeter()
    with pytest.raises(SkillNotFound):
        await g.invoke("nope", _ctx())


async def test_invoke_handler_error_wrapped():
    g = _Greeter()
    with pytest.raises(SkillInvocationError, match="ValueError"):
        await g.invoke("boom", _ctx())


class _CollectInput(BaseModel):
    vendor: str
    total: float


async def test_collect_emits_schema_and_resumes_with_typed_input():
    ctx = _ctx()

    async def wait_for_event_and_answer() -> None:
        for _ in range(100):
            events = [e for e in ctx.events if e.kind == "input_request"]
            if events:
                req_id = events[0].payload["request_id"]
                assert events[0].payload["schema"]["required"] == ["vendor", "total"]
                assert RunContext.submit_input(
                    req_id, {"vendor": "Acme", "total": 42.50}
                )
                return
            await asyncio.sleep(0.01)
        raise AssertionError("input_request event was not emitted")

    waiter = asyncio.create_task(wait_for_event_and_answer())
    result = await ctx.collect(
        _CollectInput,
        title="Invoice details",
        reason="Need structured invoice fields",
    )
    await waiter

    assert isinstance(result, _CollectInput)
    assert result.vendor == "Acme"
    assert result.total == 42.50


def test_local_context_exposes_consumer_setup_values():
    ctx = LocalRunContext(
        auth=NoAuth(),
        consumer_config={"DEFAULT_REPO": "acme/api"},
        consumer_secrets={"GITHUB_TOKEN": "ghp_secret"},
    )

    assert ctx.consumer_config("DEFAULT_REPO") == "acme/api"
    assert ctx.consumer_config("MISSING", "fallback") == "fallback"
    assert ctx.consumer_secret("GITHUB_TOKEN") == "ghp_secret"
    with pytest.raises(ConsumerSetupMissing):
        ctx.consumer_secret("MISSING")


class _Pair(BaseModel):
    a: int
    b: int


class _PairCfg(BaseModel):
    pass


class _PairAgent(A2AAgent[_PairCfg, NoAuth]):
    name = "json-out"
    description = ""

    @skill()
    async def make(self, ctx: RunContext[NoAuth]) -> _Pair:
        return _Pair(a=1, b=2)


async def test_invoke_json_returns_serializable():
    out = await _PairAgent().invoke_json("make", LocalRunContext(auth=NoAuth()), {})
    assert out == {"a": 1, "b": 2}


# --- scopes ---

async def test_scope_enforcement_blocks_caller():
    class _Cfg(BaseModel):
        pass

    class _S(A2AAgent[_Cfg, JWTAuth]):
        name = "scoped-agent"
        description = ""
        auth_model = JWTAuth

        @skill(scopes=["admin"])
        async def secret_op(self, ctx: RunContext[JWTAuth]) -> str:
            return "ok"

    bad = LocalRunContext(auth=JWTAuth(sub="alice", scopes=["read"]))
    with pytest.raises(MissingScopes):
        await _S().invoke("secret_op", bad)

    good = LocalRunContext(auth=JWTAuth(sub="alice", scopes=["admin"]))
    assert await _S().invoke("secret_op", good) == "ok"


async def test_scope_enforcement_allows_no_scope_skill():
    g = _Greeter()
    assert await g.invoke("greet", _ctx(), who="bob") == "hello bob!"


# --- streaming helpers ---

async def test_stream_helpers_emit_typed_events():
    ctx = _ctx()
    await ctx.emit_text_delta("hello ")
    await ctx.emit_text_delta("world")
    await ctx.emit_error("uh oh", code="E001")
    kinds = [e.kind for e in ctx.events]
    assert kinds == ["text_delta", "text_delta", "error"]
    assert ctx.events[-1].payload == {"message": "uh oh", "code": "E001"}


# --- health ---

async def test_default_health_is_true():
    assert await _Greeter().health() is True


# --- local_invoke ---

async def test_local_invoke_default_no_auth():
    g = _Greeter()
    assert await g.local_invoke("greet", who="bob") == "hello bob!"


async def test_local_invoke_with_explicit_auth():
    class _Cfg(BaseModel):
        pass

    class _A(A2AAgent[_Cfg, JWTAuth]):
        name = "auth-test"
        description = ""
        auth_model = JWTAuth

        @skill()
        async def whoami(self, ctx: RunContext[JWTAuth]) -> str:
            return f"{ctx.auth.sub}@{ctx.auth.org_id}"

    out = await _A().local_invoke(
        "whoami", auth=JWTAuth(sub="alice", org_id="acme")
    )
    assert out == "alice@acme"


# --- artifacts / inheritance ---

async def test_artifact_round_trip():
    class _Cfg(BaseModel):
        pass

    class _A(A2AAgent[_Cfg, NoAuth]):
        name = "art"
        description = ""

        @skill()
        async def write(self, ctx: RunContext[NoAuth], body: str) -> str:
            ref = await ctx.write_artifact("note.txt", body.encode(), "text/plain")
            await ctx.emit_artifact(ref)
            return ref.uri

    ctx = _ctx()
    uri = await _A().invoke("write", ctx, body="hello")
    assert uri.startswith("memory://")
    assert ctx.artifacts["note.txt"] == b"hello"
    assert any(e.kind == "artifact" for e in ctx.events)


def test_skill_inheritance_preserves_parent_skills():
    class _Loud(_Greeter):
        name = "loud-greeter"

        @skill(description="Shout")
        async def shout(self, ctx: RunContext[NoAuth], what: str) -> str:
            return what.upper()

    skills = _Loud().skills
    assert set(skills) == {"greet", "boom", "shout"}


# --- runtime metadata ---


class _SessionState(BaseModel):
    history: list[str] = []


def test_default_runtime_is_ephemeral_no_state_microsandbox():
    rt = _Greeter.runtime()
    assert rt.lifecycle is Lifecycle.EPHEMERAL
    assert rt.state is State.NONE
    assert rt.sandbox is Sandbox.MICROSANDBOX  # safe-by-default
    assert rt.concurrency == 1


def test_state_requires_state_model():
    with pytest.raises(TypeError, match="state_model"):

        class _Bad(A2AAgent):
            name = "bad"
            description = ""
            state = State.SESSION


def test_ephemeral_lifecycle_incompatible_with_session_state():
    with pytest.raises(TypeError, match="ephemeral.*session"):

        class _Bad(A2AAgent):
            name = "bad"
            description = ""
            lifecycle = Lifecycle.EPHEMERAL
            state = State.SESSION
            state_model = _SessionState


def test_runtime_metadata_propagates_to_card():
    class _ChatCfg(BaseModel):
        pass

    class _Chat(A2AAgent[_ChatCfg, JWTAuth]):
        name = "chat"
        description = "stateful chat agent"
        auth_model = JWTAuth
        lifecycle = Lifecycle.SESSION
        state = State.SESSION
        state_model = _SessionState
        resources = Resources(cpu="2", memory="4Gi", gpu=0, max_runtime_seconds=1800)
        concurrency = 4
        egress = EgressPolicy(
            allow_hosts=("api.openai.com",),
            allow_internal_services=("litellm.llm.svc.cluster.local",),
        )
        tools_used = ("litellm", "minio")

        @skill(timeout_seconds=30, idempotent=True, max_retries=2, cost_class="cheap")
        async def reply(self, ctx: RunContext[JWTAuth], message: str) -> str:
            return f"echo {message}"

    card = _Chat().card()
    assert card.runtime.lifecycle is Lifecycle.SESSION
    assert card.runtime.state is State.SESSION
    assert card.runtime.sandbox is Sandbox.MICROSANDBOX
    assert card.runtime.resources.cpu == "2"
    assert card.runtime.concurrency == 4
    assert card.runtime.egress.allow_hosts == ("api.openai.com",)
    assert card.runtime.tools_used == ("litellm", "minio")
    assert card.state_schema is not None
    assert "history" in card.state_schema["properties"]

    skill_card = card.skills[0]
    assert skill_card.policy.timeout_seconds == 30
    assert skill_card.policy.idempotent is True
    assert skill_card.policy.max_retries == 2
    assert skill_card.policy.cost_class == "cheap"


def test_skill_metadata_propagates_to_card():
    class _ScopedConfig(BaseModel):
        pass

    class _Scoped(A2AAgent[_ScopedConfig, APIKeyAuth]):
        name = "scoped"
        description = "scope test"
        config_model = _ScopedConfig
        auth_model = APIKeyAuth

        @skill(scopes=["a:read", "a:write"], stream=True, tags=["x"])
        async def do(self, ctx: RunContext[APIKeyAuth]) -> str:
            return "ok"

    card = _Scoped().card()
    s = card.skills[0]
    assert s.scopes == ["a:read", "a:write"]
    assert s.stream is True
    assert s.tags == ["x"]


def test_local_run_context_caller_and_grant_ids_default_empty():
    ctx = LocalRunContext(auth=NoAuth())
    assert ctx.caller == ""
    assert ctx.grant_ids == ()


def test_local_run_context_caller_and_grant_ids_round_trip():
    ctx = LocalRunContext(
        auth=NoAuth(),
        caller="user:42",
        grant_ids=("g_abc", "g_def"),
    )
    assert ctx.caller == "user:42"
    assert ctx.grant_ids == ("g_abc", "g_def")


def test_auth_resolver_principal_id_defaults():
    from a2a_pack.auth import (
        APIKeyAuthResolver,
        NoAuthResolver,
        PlatformUserAuthResolver,
        RemoteBearerAuthResolver,
        StaticAuthResolver,
    )
    from a2a_pack import PlatformUserAuth as _PU

    assert NoAuthResolver.principal_id(NoAuth()) == ""
    assert StaticAuthResolver.principal_id(NoAuth()) == ""
    assert APIKeyAuthResolver.principal_id(
        APIKeyAuth(api_key_id="abcdef0123456789", scopes=[])
    ) == "apikey:abcdef012345"
    assert PlatformUserAuthResolver.principal_id(
        _PU(sub="local-dev", user_id=7, email="x@y.z")
    ) == "7"
    assert PlatformUserAuthResolver.principal_id(
        _PU(sub="local-dev", email="x@y.z")
    ) == "local-dev"
    assert RemoteBearerAuthResolver.principal_id(
        JWTAuth(sub="alice", email="a@example.com")
    ) == "alice"

def test_tool_is_documented_alias_of_skill() -> None:
    """@tool is the documented name; @skill must stay importable forever —
    existing published agents decorate with it."""
    from a2a_pack import skill as skill_export, tool as tool_export
    from a2a_pack.agent import skill as skill_def, tool as tool_def

    assert tool_export is skill_export
    assert tool_def is skill_def
