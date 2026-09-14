from __future__ import annotations

import inspect
import os
import time
import typing
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, ClassVar, Generic, Sequence, TypeVar

from pydantic import BaseModel, TypeAdapter
from pydantic.fields import FieldInfo

from .auth import AuthResolver, NoAuth
from .card import AgentCard
from .consumer_setup import ConsumerSetup
from .mail import InboundEmailPayload
from .context import AgentEvent, LocalRunContext, RunContext, ScopeDenied, ScopeExpansionNotAllowed
from .composition import ensure_composition_budget
from .receipts import hash_input, seal_receipt
from .replay import EventRecorder, seal_replay_session
from .runtime import (
    AccountAccess,
    AgentEndpoint,
    AgentRuntime,
    AgentPlatformResources,
    MetaAgentManifest,
    EgressPolicy,
    Lifecycle,
    LLMProvisioning,
    Resources,
    RuntimeAvailability,
    Sandbox,
    SkillPolicy,
    State,
    TemplateLineage,
)
from .workspace import FileUpload, WorkspaceAccess

ConfigT = TypeVar("ConfigT", bound=BaseModel)
AuthT = TypeVar("AuthT", bound=BaseModel)


_RESERVED_PARAM_NAMES = frozenset({"self", "ctx", "context"})

#: Literal tag stamped on the compiled skill card of an ``on_email=True``
#: skill so the control plane can discover the agent's email handler.
EMAIL_HANDLER_TAG = "a2a:email-handler"


class _EmptyConfig(BaseModel):
    """Default config model when an agent declares no config."""


class SkillNotFound(KeyError):
    """Raised when invoke() is called with an unknown skill name."""


class SkillInvocationError(RuntimeError):
    """Raised when a skill handler raises during invoke()."""


class SkillInputError(ValueError):
    """Raised when invoke() inputs fail validation against the skill schema."""


@dataclass(frozen=True)
class ParamSpec:
    """Validation metadata for a single skill parameter."""

    name: str
    adapter: TypeAdapter[Any]
    has_default: bool
    default: Any = None


@dataclass(frozen=True)
class SkillSpec:
    """Static metadata about a single skill, captured at decoration time."""

    name: str
    description: str
    tags: tuple[str, ...]
    scopes: tuple[str, ...]
    stream: bool
    policy: SkillPolicy
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    handler: Callable[..., Awaitable[Any]]
    params: tuple[ParamSpec, ...] = field(default_factory=tuple)
    output_adapter: TypeAdapter[Any] | None = None


def skill(
    *,
    name: str | None = None,
    description: str = "",
    tags: Sequence[str] = (),
    scopes: Sequence[str] = (),
    stream: bool = False,
    on_email: bool = False,
    input_schema: dict[str, Any] | None = None,
    timeout_seconds: float | None = None,
    idempotent: bool = False,
    max_retries: int = 0,
    cost_class: str | None = None,
    allow_scope_expansion: bool = False,
    grant_mode: str | None = None,
    grant_allow_patterns: Sequence[str] = (),
    grant_deny_patterns: Sequence[str] = (),
    grant_outputs_prefix: str | None = None,
    grant_write_prefixes: Sequence[str] = (),
    grant_ttl_seconds: int | None = None,
    grant_run_timeout_seconds: int | None = None,
    grant_approval_timeout_seconds: int | None = None,
    grant_scope_approval_timeout_seconds: int | None = None,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Mark an :class:`A2AAgent` method as a callable, typed tool.

    Canonical style is namespaced — ``import a2a_pack as a2a`` then
    ``@a2a.tool(...)`` — so it never collides with other libraries' bare
    ``@tool`` (LangChain et al.). ``@skill`` is a permanent alias kept for
    existing agents and because the A2A wire format calls these entries
    ``skills`` in the agent card. All names are the same object.

    Conventions:

    - The handler MUST be ``async def``.
    - Its first parameter (after ``self``) MUST be a :class:`RunContext`;
      the context is supplied by the runtime and is omitted from the
      published input schema.
    - Remaining parameters MUST be type-annotated. ``*args`` and ``**kwargs``
      are rejected.
    - ``input_schema`` overrides the *published* schema (agent card, MCP tools)
      when the signature-derived one is too loose — e.g. generated wrappers whose
      Python signature is ``parameters: dict, body: Any`` but whose real contract
      is a specific OpenAPI operation. Runtime validation still uses the
      signature's type adapters; the override only changes what consumers see.
    - ``on_email=True`` declares this skill as the agent's EMAIL HANDLER:
      the platform invokes it with ``{"email": {...}}`` whenever the agent's
      mailbox receives mail. The handler signature is forced to exactly
      ``(ctx, email)`` where ``email`` is annotated
      :class:`~a2a_pack.mail.InboundEmailPayload` (or ``dict`` /
      ``dict[str, Any]``); anything else raises at decoration time. The
      compiled skill card is tagged ``a2a:email-handler`` so the control
      plane can discover it, and at most one skill per agent may set it.

      Return contract (documented, not enforced by the SDK): return ``str``
      for a plain reply body, ``{"body": str, "subject": str (optional)}``
      for an explicit reply, or ``None``/empty for no email reply.
    """

    def decorator(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(
                f"@tool requires an async function: {fn.__qualname__}"
            )

        sig = inspect.signature(fn)
        hints = typing.get_type_hints(fn, include_extras=True)
        params = list(sig.parameters.values())[1:]  # drop self
        if not params:
            raise TypeError(
                f"@tool {fn.__qualname__}: missing RunContext parameter"
            )

        ctx_param, *rest = params
        ctx_hint = hints.get(ctx_param.name)
        if ctx_hint is None or not _is_run_context(ctx_hint):
            raise TypeError(
                f"@tool {fn.__qualname__}: first arg after self must be "
                f"annotated as RunContext (got {ctx_hint!r})"
            )

        if on_email:
            _enforce_email_handler_signature(fn, rest, hints)

        properties: dict[str, Any] = {}
        required: list[str] = []
        param_specs: list[ParamSpec] = []
        for p in rest:
            if p.kind is inspect.Parameter.VAR_POSITIONAL:
                raise TypeError(
                    f"@tool {fn.__qualname__}: *{p.name} is not allowed"
                )
            if p.kind is inspect.Parameter.VAR_KEYWORD:
                raise TypeError(
                    f"@tool {fn.__qualname__}: **{p.name} is not allowed"
                )
            if p.name in _RESERVED_PARAM_NAMES:
                raise TypeError(
                    f"@tool {fn.__qualname__}: reserved param name {p.name!r}"
                )
            if p.name not in hints:
                raise TypeError(
                    f"@tool {fn.__qualname__}: parameter {p.name!r} is "
                    f"missing a type annotation"
                )
            tp = hints[p.name]
            field_info = p.default if isinstance(p.default, FieldInfo) else None
            adapter_type = typing.Annotated[tp, field_info] if field_info is not None else tp
            adapter: TypeAdapter[Any] = TypeAdapter(adapter_type)
            schema = adapter.json_schema()
            schema.update(_file_upload_schema_extra(tp))
            properties[p.name] = schema
            has_default = p.default is not inspect.Parameter.empty
            if field_info is not None:
                has_default = not field_info.is_required()
            if not has_default:
                required.append(p.name)
            param_specs.append(
                ParamSpec(
                    name=p.name,
                    adapter=adapter,
                    has_default=has_default,
                    default=None if not has_default else p.default,
                )
            )

        effective_tags = tuple(tags)
        if on_email:
            # The platform builds the payload, so publish a permissive object
            # schema and validate at runtime as a plain dict — the sidecar
            # only checks top-level parameter names, and the payload may gain
            # fields without breaking deployed agents.
            properties["email"] = {"type": "object", "additionalProperties": True}
            param_specs = [
                ParamSpec(
                    name="email",
                    adapter=TypeAdapter(dict[str, Any]),
                    has_default=False,
                )
            ]
            required = ["email"]
            if EMAIL_HANDLER_TAG not in effective_tags:
                effective_tags = (*effective_tags, EMAIL_HANDLER_TAG)

        derived_schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }
        if input_schema is not None:
            if not isinstance(input_schema, dict) or input_schema.get("type") != "object":
                raise TypeError(
                    f"@tool {fn.__qualname__}: input_schema override must be a "
                    f"JSON Schema object (type: object)"
                )
            # Published contract only — runtime validation stays on the
            # signature adapters, so the override cannot widen what the
            # handler actually accepts.
            derived_schema = input_schema
        return_tp = hints.get("return", Any)
        output_adapter: TypeAdapter[Any] = TypeAdapter(return_tp)

        spec = SkillSpec(
            name=name or fn.__name__,
            description=description,
            tags=effective_tags,
            scopes=tuple(scopes),
            stream=stream,
            policy=SkillPolicy(
                timeout_seconds=timeout_seconds,
                idempotent=idempotent,
                max_retries=max_retries,
                cost_class=cost_class,
                allow_scope_expansion=allow_scope_expansion,
                grant_mode=grant_mode,
                grant_allow_patterns=tuple(grant_allow_patterns),
                grant_deny_patterns=tuple(grant_deny_patterns),
                grant_outputs_prefix=grant_outputs_prefix,
                grant_write_prefixes=tuple(grant_write_prefixes),
                grant_ttl_seconds=grant_ttl_seconds,
                grant_run_timeout_seconds=grant_run_timeout_seconds,
                grant_approval_timeout_seconds=grant_approval_timeout_seconds,
                grant_scope_approval_timeout_seconds=grant_scope_approval_timeout_seconds,
            ),
            input_schema=derived_schema,
            output_schema=output_adapter.json_schema(),
            handler=fn,
            params=tuple(param_specs),
            output_adapter=output_adapter,
        )
        fn.__a2a_skill__ = spec  # type: ignore[attr-defined]
        return fn

    return decorator


# Documented name. "Skill" now widely means prompt-pack folders (e.g. Claude
# Skills); these are typed callables, which the ecosystem calls tools — and
# every one is already served as an MCP tool. The agent-card field stays
# `skills` (A2A spec vocabulary), so @skill remains a supported alias forever.
tool = skill


def _enforce_email_handler_signature(
    fn: Callable[..., Any],
    business_params: list[inspect.Parameter],
    hints: dict[str, Any],
) -> None:
    """Enforce the forced ``(ctx, email)`` shape of an ``on_email`` skill.

    Exactly one business parameter named ``email``, annotated
    :class:`~a2a_pack.mail.InboundEmailPayload` or ``dict``/``dict[str, Any]``.
    """
    problem: str | None = None
    if len(business_params) != 1 or business_params[0].name != "email":
        got = ", ".join(p.name for p in business_params) or "<none>"
        problem = f"got business parameter(s): {got}"
    else:
        tp = hints.get("email")
        if not _is_email_payload_annotation(tp):
            problem = f"parameter 'email' has unsupported annotation {tp!r}"
    if problem is not None:
        raise TypeError(
            f"@tool(on_email=True) {fn.__qualname__}: email handler must "
            f"have signature (ctx, email: InboundEmailPayload) — {problem}"
        )


def _is_email_payload_annotation(tp: Any) -> bool:
    """True for ``InboundEmailPayload``, ``dict`` and ``dict[str, Any]``."""
    if tp is InboundEmailPayload or tp is dict:
        return True
    if typing.get_origin(tp) is dict:
        args = typing.get_args(tp)
        return not args or (args[0] is str and args[1] is Any)
    return False


def _is_run_context(tp: Any) -> bool:
    """True if ``tp`` is :class:`RunContext` or a parametrization of it."""
    origin = typing.get_origin(tp) or tp
    try:
        return isinstance(origin, type) and issubclass(origin, RunContext)
    except TypeError:
        return False


def _file_upload_schema_extra(tp: Any) -> dict[str, Any]:
    """Return JSON Schema extension metadata from ``Annotated[..., FileUpload]``."""

    if typing.get_origin(tp) is not typing.Annotated:
        return {}
    for item in typing.get_args(tp)[1:]:
        if isinstance(item, FileUpload):
            return item.schema_extra()
    return {}


class _AgentMeta(type):
    def __new__(mcs, cls_name, bases, namespace):
        cls = super().__new__(mcs, cls_name, bases, namespace)
        skills: dict[str, SkillSpec] = {}
        for base in bases:
            skills.update(getattr(base, "_skills", {}))
        for attr in namespace.values():
            spec = getattr(attr, "__a2a_skill__", None)
            if spec is None:
                continue
            if spec.name in skills and skills[spec.name].handler is not spec.handler:
                # Allow overrides from the same chain (parent → child) but
                # forbid two distinct handlers in the same class.
                if any(
                    spec.name in getattr(b, "_skills", {})
                    and getattr(b, "_skills")[spec.name].handler is spec.handler
                    for b in bases
                ):
                    pass  # legitimate override
                else:
                    raise TypeError(
                        f"duplicate skill name {spec.name!r} in {cls_name}"
                    )
            skills[spec.name] = spec
        email_handlers = sorted(
            spec.name for spec in skills.values() if EMAIL_HANDLER_TAG in spec.tags
        )
        if len(email_handlers) > 1:
            raise TypeError(
                f"{cls_name}: at most one skill may be the email handler "
                f"(on_email=True), got {email_handlers}"
            )
        cls._skills = skills  # type: ignore[attr-defined]
        return cls


class A2AAgent(Generic[ConfigT, AuthT], metaclass=_AgentMeta):
    """Base class for A2A agents.

    Subclasses declare:

    - ``name``, ``description`` (and optional ``version``),
    - optional ``config_model`` / ``auth_model`` (default to empty / NoAuth),
    - deployment metadata: ``required_secrets``, ``required_env``,
      ``capabilities``, ``input_modes``, ``output_modes``,
    - one or more methods decorated with :func:`skill`.
    """

    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    version: ClassVar[str] = "0.1.0"

    config_model: ClassVar[type[BaseModel]] = _EmptyConfig
    auth_model: ClassVar[type[BaseModel]] = NoAuth
    auth_resolver: ClassVar[AuthResolver[Any] | None] = None

    required_secrets: ClassVar[tuple[str, ...]] = ()
    required_env: ClassVar[tuple[str, ...]] = ()
    consumer_setup: ClassVar[ConsumerSetup] = ConsumerSetup.none()
    capabilities: ClassVar[dict[str, Any]] = {}
    meta_agent_manifest: ClassVar[MetaAgentManifest] = MetaAgentManifest()
    template_lineage: ClassVar[TemplateLineage] = TemplateLineage.none()
    input_modes: ClassVar[tuple[str, ...]] = ("application/json",)
    output_modes: ClassVar[tuple[str, ...]] = ("application/json",)

    # --- runtime / deployment declaration (read by the platform deployer) ---
    # Sandbox is always microsandbox; not exposed as a knob.
    lifecycle: ClassVar[Lifecycle] = Lifecycle.EPHEMERAL
    availability: ClassVar[RuntimeAvailability] = RuntimeAvailability.ON_DEMAND
    state: ClassVar[State] = State.NONE
    state_model: ClassVar[type[BaseModel] | None] = None
    resources: ClassVar[Resources] = Resources()
    concurrency: ClassVar[int] = 1
    egress: ClassVar[EgressPolicy] = EgressPolicy()
    tools_used: ClassVar[tuple[str, ...]] = ()
    workspace_access: ClassVar[WorkspaceAccess] = WorkspaceAccess.none()
    # Where the agent's LLM credentials come from. Drives both runtime
    # ``ctx.llm`` resolution.
    llm_provisioning: ClassVar[LLMProvisioning] = LLMProvisioning.PLATFORM
    # Optional account gate with N platform-funded skill calls per account.
    # Once exhausted, the caller must have a saved LLM credential (BYOK).
    account_access: ClassVar[AccountAccess] = AccountAccess()
    platform_resources: ClassVar[AgentPlatformResources] = AgentPlatformResources()
    endpoints: ClassVar[tuple[AgentEndpoint, ...]] = ()
    # Trust the platform to forward the caller's CP JWT (so ``ctx.cp_jwt``
    # is populated and the skill can call back into CP endpoints on the
    # caller's behalf). Off by default — only platform-trusted agents
    # like ``agent-builder`` should opt in.
    wants_cp_jwt: ClassVar[bool] = False

    _skills: ClassVar[dict[str, SkillSpec]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not cls.name:
            raise TypeError(
                f"{cls.__name__}.name must be set as a class attribute"
            )
        if cls.state is not State.NONE and cls.state_model is None:
            raise TypeError(
                f"{cls.__name__} declares state={cls.state.value!r} but "
                f"state_model is not set"
            )
        if cls.lifecycle is Lifecycle.EPHEMERAL and cls.state is State.SESSION:
            raise TypeError(
                f"{cls.__name__}: lifecycle=ephemeral is incompatible with "
                f"state=session"
            )

    @classmethod
    def runtime(cls) -> AgentRuntime:
        """Aggregate the class-level runtime declaration.

        ``sandbox`` is always :attr:`Sandbox.MICROSANDBOX`; it is set here
        rather than on the class so developers cannot weaken isolation.
        """
        return AgentRuntime(
            lifecycle=cls.lifecycle,
            availability=cls.availability,
            state=cls.state,
            sandbox=Sandbox.MICROSANDBOX,
            resources=cls.resources,
            concurrency=cls.concurrency,
            egress=cls.egress,
            tools_used=cls.tools_used,
            llm_provisioning=cls.llm_provisioning,
            account_access=cls.account_access,
            wants_cp_jwt=cls.wants_cp_jwt,
            platform_resources=cls.platform_resources,
            endpoints=cls.endpoints,
        )

    def __init__(self, config: ConfigT | dict[str, Any] | None = None) -> None:
        validated = type(self).config_model.model_validate(config or {})
        self.config: ConfigT = typing.cast(ConfigT, validated)

    @property
    def skills(self) -> dict[str, SkillSpec]:
        return type(self)._skills

    async def startup(self, ctx: RunContext[AuthT]) -> None:
        """Called once before the first invocation. Override to set up state."""

    async def shutdown(self, ctx: RunContext[AuthT]) -> None:
        """Called once before the agent process exits. Override to tear down."""

    async def health(self) -> bool:
        """Lightweight liveness check. Override to add real probes."""
        return True

    async def invoke(
        self,
        skill_name: str,
        ctx: RunContext[AuthT],
        /,
        **kwargs: Any,
    ) -> Any:
        """Invoke a skill with caller-supplied kwargs.

        Inputs are validated and coerced via the skill's pydantic schema.
        Required scopes are enforced against ``ctx.auth`` before the handler
        runs. The raw handler return value is returned (Python-typed).
        """
        spec = self.skills.get(skill_name)
        if spec is None:
            raise SkillNotFound(skill_name)

        previous_agent_name = getattr(ctx, "_current_agent_name", None)
        try:
            object.__setattr__(ctx, "_current_agent_name", type(self).name)
            object.__setattr__(
                ctx,
                "_composition_budget",
                ensure_composition_budget(
                    getattr(ctx, "_composition_budget", None),
                    current_agent=type(self).name,
                    llm_budget_usd=_context_grant_llm_budget(ctx),
                ),
            )
        except (AttributeError, TypeError):
            pass

        ctx.require_scopes(spec.scopes)
        # Stash skill-level policy bits the context needs to gate runtime
        # capabilities. Keep this *after* require_scopes so a denied request
        # never sees the flag.
        try:
            object.__setattr__(
                ctx,
                "_scope_expansion_allowed",
                spec.policy.allow_scope_expansion,
            )
        except (AttributeError, TypeError):
            # Frozen / slotted contexts: skill simply can't negotiate scope.
            pass

        try:
            validated = self._validate_inputs(spec, kwargs)
        except Exception as exc:
            raise SkillInputError(
                f"invalid input for skill {spec.name!r}: {exc}"
            ) from exc

        started_at = int(time.time())
        input_hash = hash_input(validated)
        recorder = EventRecorder(
            agent_name=self.name,
            agent_version=getattr(self, "version", ""),
            skill_name=spec.name,
            caller=getattr(ctx, "caller", "") or "",
            task_id=getattr(ctx, "task_id", "") or "",
            input_hash=input_hash,
            random_seed=getattr(ctx, "random_seed", "") or "",
        )
        recorder.record("skill_start", {"args_hash": input_hash, "args": validated})
        # Stash on ctx so workspace/llm/tool sites can append events as
        # they run. ``object.__setattr__`` lets us write past frozen/slotted
        # contexts without breaking the surface.
        try:
            object.__setattr__(ctx, "_replay_recorder", recorder)
        except (AttributeError, TypeError):
            pass

        result: Any = None
        status = "ok"
        error_type = ""
        raised: BaseException | None = None
        try:
            try:
                result = await spec.handler(self, ctx, **validated)
            except SkillInputError as exc:
                status = "error"
                error_type = type(exc).__name__
                raised = exc
                raise
            except (ScopeDenied, ScopeExpansionNotAllowed) as exc:
                # Scope-negotiation failures carry meaningful error semantics
                # for the caller and the platform — let them propagate
                # unwrapped.
                status = "error"
                error_type = type(exc).__name__
                raised = exc
                raise
            except Exception as exc:
                status = "error"
                error_type = type(exc).__name__
                raised = exc
                raise SkillInvocationError(
                    f"skill {spec.name!r} raised {type(exc).__name__}: {exc}"
                ) from exc
        finally:
            if previous_agent_name is not None:
                try:
                    object.__setattr__(ctx, "_current_agent_name", previous_agent_name)
                except (AttributeError, TypeError):
                    pass
            if raised is None:
                recorder.record("skill_end", {"status": "ok"})
            else:
                recorder.record("error", {"type": type(raised).__name__})

            await self._emit_receipt_and_replay(
                ctx=ctx,
                spec=spec,
                started_at=started_at,
                validated=validated,
                result=result,
                status=status,
                error_type=error_type,
                recorder=recorder,
            )

        return result

    async def _emit_receipt_and_replay(
        self,
        *,
        ctx: RunContext[AuthT],
        spec: SkillSpec,
        started_at: int,
        validated: dict[str, Any],
        result: Any,
        status: str,
        error_type: str,
        recorder: EventRecorder,
    ) -> None:
        """Best-effort seal + emit.

        Evidence signing requires configured Ed25519 runtime keys. Signing
        failures must never mask the real handler outcome; they are surfaced
        via a debug-grade event.
        """
        # Hosted platform ingress is sealed by a trusted gateway after the
        # response completes. User workloads intentionally do not receive the
        # platform private keys, and must not emit a misleading local
        # ``receipt_error`` (or an untrusted self-signed evidence event) on
        # that path.
        if os.environ.get("A2A_EVIDENCE_SIGNING_MODE", "").strip().lower() == "gateway":
            return
        try:
            receipt, receipt_token = seal_receipt(
                agent_name=self.name,
                skill_name=spec.name,
                started_at=started_at,
                agent_version=getattr(self, "version", ""),
                caller=getattr(ctx, "caller", "") or "",
                task_id=getattr(ctx, "task_id", "") or "",
                grant_ids=tuple(getattr(ctx, "grant_ids", ()) or ()),
                inputs=validated,
                result=result,
                status=status,
                error_type=error_type,
            )
            await ctx.emit_event(
                AgentEvent(
                    kind="receipt_sealed",
                    payload={
                        "token": receipt_token,
                        "receipt_id": receipt.receipt_id,
                    },
                )
            )
            session = recorder.build_session(receipt_id=receipt.receipt_id)
            _, session_token = seal_replay_session(session)
            await ctx.emit_event(
                AgentEvent(
                    kind="replay_sealed",
                    payload={
                        "token": session_token,
                        "session_id": session.session_id,
                        "receipt_id": receipt.receipt_id,
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001
            # Surface but do not fail the invocation.
            try:
                await ctx.emit_event(
                    AgentEvent(
                        kind="receipt_error",
                        payload={"message": str(exc), "type": type(exc).__name__},
                    )
                )
            except Exception:  # noqa: BLE001
                pass

    async def invoke_json(
        self,
        skill_name: str,
        ctx: RunContext[AuthT],
        payload: dict[str, Any],
    ) -> Any:
        """Runtime-facing invoke: takes JSON-shaped payload, returns JSON-shaped result."""
        spec = self.skills.get(skill_name)
        if spec is None:
            raise SkillNotFound(skill_name)
        result = await self.invoke(skill_name, ctx, **payload)
        if spec.output_adapter is None:
            return result
        return spec.output_adapter.dump_python(result, mode="json")

    async def local_invoke(
        self,
        skill_name: str,
        /,
        *,
        auth: AuthT | None = None,
        secrets: dict[str, str] | None = None,
        task_id: str = "local-task",
        workspace: Any = None,  # WorkspaceClient or None
        sandbox: Any = None,  # SandboxClient or None
        a2a: Any = None,  # A2AClient or None
        discover: Any = None,  # DiscoveryClient or None
        consumer_config: dict[str, Any] | None = None,
        consumer_secrets: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Convenience harness: build a :class:`LocalRunContext` and invoke.

        Useful in tests and notebooks. Pass ``workspace=``, ``sandbox=``,
        ``a2a=``, and/or ``discover=`` to bind concrete runtime clients.
        """
        if auth is None:
            auth = typing.cast(AuthT, type(self).auth_model())
        ctx: LocalRunContext[AuthT] = LocalRunContext(
            auth=auth,
            secrets=secrets,
            task_id=task_id,
            workspace=workspace,
            sandbox=sandbox,
            a2a=a2a,
            discover=discover,
            consumer_config=consumer_config,
            consumer_secrets=consumer_secrets,
        )
        return await self.invoke(skill_name, ctx, **kwargs)

    def card(self) -> AgentCard:
        return AgentCard.from_agent(self)

    @staticmethod
    def _validate_inputs(
        spec: SkillSpec, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        known = {p.name for p in spec.params}
        unknown = set(kwargs) - known
        if unknown:
            raise ValueError(f"unknown parameters: {sorted(unknown)}")

        validated: dict[str, Any] = {}
        for p in spec.params:
            if p.name in kwargs:
                validated[p.name] = p.adapter.validate_python(kwargs[p.name])
            elif not p.has_default:
                raise ValueError(f"missing required parameter: {p.name!r}")
            elif isinstance(p.default, FieldInfo):
                default = p.default.get_default(call_default_factory=True)
                validated[p.name] = p.adapter.validate_python(default)
            else:
                validated[p.name] = p.adapter.validate_python(p.default)
        return validated


def _context_grant_llm_budget(ctx: RunContext[Any]) -> float | None:
    workspace = getattr(ctx, "_workspace", None)
    grant = getattr(workspace, "current_grant", None)
    value = getattr(grant, "llm_max_budget_usd", None)
    return float(value) if value is not None else None
