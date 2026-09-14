"""Runtime context handed to skill handlers.

The same agent code runs unchanged on local dev, Docker, Kubernetes, and
hosted runtimes — the runtime provides a concrete :class:`RunContext` that
implements artifact storage, secret access, streaming, and cancellation.
"""
from __future__ import annotations

import asyncio
import fnmatch
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Generic, Sequence, TypeVar

from pydantic import BaseModel, TypeAdapter

from dataclasses import dataclass as _dataclass

from .a2a_client import A2AClient, CallResult
from .discovery import DiscoveryClient
from .composition import (
    CompositionBudget,
    CompositionLimitExceeded,
    ensure_composition_budget,
)
from .grants import Grant, GrantInvalid, verify_grant
from .sandbox import ExecResult, SandboxClient, SandboxSpec, SandboxUnavailable
from .workspace import (
    WorkspaceClient,
    WorkspaceMode,
    _matches_write_prefix,
    _normalize_write_prefixes,
)


@_dataclass(frozen=True)
class LLMCreds:
    """An LLM endpoint + credentials handed to the skill at runtime.

    Resolution order — checked in :meth:`RunContext.llm`:

      1. ``llm_creds`` in the inbound /invoke body — set by the control
         plane from the caller's saved LLM credential.
      2. Per-agent env vars: ``AGENT_LLM_{URL,KEY,MODEL}`` for
         ``llm_provisioning=agent_byok``.
      3. Local platform env: ``A2A_LITELLM_{URL,KEY,MODEL}`` for trusted
         platform-owned services and local development.

    Skill code shouldn't care which branch fired — just read
    ``ctx.llm`` and pass it through to your chat client.
    """
    base_url: str
    api_key: str
    model: str
    source: str  # "caller" | "agent_byok" | "platform"
    temperature_mode: str = "default"
    temperature: float | None = None
    extra_body: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None

AuthT = TypeVar("AuthT", bound=BaseModel)


class CancelledByCaller(RuntimeError):
    """Raised by :meth:`RunContext.check_cancelled` when the caller cancelled."""


class MissingScopes(PermissionError):
    """Raised by :meth:`RunContext.require_scopes` when caller lacks scopes."""

    def __init__(self, missing: Sequence[str]) -> None:
        self.missing = tuple(missing)
        super().__init__(f"missing scopes: {sorted(self.missing)}")


class ConsumerSetupMissing(KeyError):
    """Raised when a skill reads a caller setup value that was not provided."""


class ScopeDenied(PermissionError):
    """Raised by :meth:`RunContext.request_scope` when the platform refuses."""


class ScopeExpansionNotAllowed(PermissionError):
    """Raised when a tool calls request_scope without opting in via @tool."""


@dataclass(frozen=True)
class ArtifactRef:
    """Opaque handle to a stored artifact (blob, file, etc.)."""

    name: str
    uri: str
    mime_type: str
    size_bytes: int


@dataclass(frozen=True)
class AgentEvent:
    """A structured event emitted during a skill run."""

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


class _AskRegistry:
    """Process-local registry of pending :meth:`RunContext.ask` calls.

    Each call gets a unique question_id; the runtime layer is responsible
    for routing the answer back via :meth:`answer`. Lives at module scope
    so the HTTP /answers handler can resolve waiters across requests.
    """

    _waiters: dict[str, "asyncio.Future[str]"] = {}

    @classmethod
    async def ask(
        cls, ctx: "RunContext[Any]", prompt: str, *, timeout: float = 180.0
    ) -> str:
        import secrets

        question_id = f"q_{secrets.token_hex(6)}"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        cls._waiters[question_id] = fut
        try:
            await ctx.emit_event(
                AgentEvent(
                    kind="question",
                    payload={
                        "question_id": question_id,
                        "prompt": prompt,
                        "timeout_seconds": timeout,
                    },
                )
            )
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            cls._waiters.pop(question_id, None)

    @classmethod
    def answer(cls, question_id: str, answer: str) -> bool:
        fut = cls._waiters.get(question_id)
        if fut is None or fut.done():
            return False
        fut.set_result(answer)
        return True


class _InputRegistry:
    """Process-local registry of pending :meth:`RunContext.collect` calls."""

    _waiters: dict[str, "asyncio.Future[dict[str, Any]]"] = {}

    @classmethod
    async def collect(
        cls,
        ctx: "RunContext[Any]",
        *,
        title: str,
        reason: str,
        schema: dict[str, Any],
        ui_schema: dict[str, Any] | None,
        timeout: float,
    ) -> dict[str, Any]:
        import secrets

        request_id = f"ir_{secrets.token_hex(6)}"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        cls._waiters[request_id] = fut
        try:
            await ctx.emit_event(
                AgentEvent(
                    kind="input_request",
                    payload={
                        "request_id": request_id,
                        "title": title,
                        "reason": reason,
                        "schema": schema,
                        "ui_schema": ui_schema or {},
                        "timeout_seconds": timeout,
                    },
                )
            )
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            cls._waiters.pop(request_id, None)

    @classmethod
    def submit(cls, request_id: str, value: dict[str, Any]) -> bool:
        fut = cls._waiters.get(request_id)
        if fut is None or fut.done():
            return False
        fut.set_result(value)
        return True


class _ScopeRegistry:
    """Process-local registry of pending :meth:`RunContext.request_scope` calls.

    Each call gets a unique request_id; the runtime layer routes the
    platform's reply back via :meth:`resolve` (delivers a fresh signed
    grant token) or :meth:`deny` (delivers a deny reason).
    """

    _waiters: dict[str, "asyncio.Future[tuple[Grant, str] | str]"] = {}

    @classmethod
    async def request(
        cls,
        ctx: "RunContext[Any]",
        *,
        reason: str,
        read: Sequence[str],
        write_prefix: str | None,
        write_prefixes: Sequence[str],
        ttl_seconds: int,
        mode: str,
        timeout: float,
        approval_timeout: float | None,
    ) -> tuple[Grant, str]:
        import secrets

        request_id = f"sr_{secrets.token_hex(6)}"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[tuple[Grant, str] | str] = loop.create_future()
        cls._waiters[request_id] = fut
        normalized_write_prefixes = _normalize_write_prefixes(
            write_prefix,
            write_prefixes,
        )
        try:
            await ctx.emit_event(
                AgentEvent(
                    kind="scope_request",
                    payload={
                        "request_id": request_id,
                        "reason": reason,
                        "read_patterns": list(read),
                        "write_prefix": write_prefix
                        or (
                            normalized_write_prefixes[0]
                            if normalized_write_prefixes
                            else None
                        ),
                        "write_prefixes": list(normalized_write_prefixes),
                        "ttl_seconds": ttl_seconds,
                        "mode": mode,
                        "timeout_seconds": timeout,
                        "approval_timeout_seconds": approval_timeout,
                    },
                )
            )
            outcome = await asyncio.wait_for(fut, timeout=timeout)
        finally:
            cls._waiters.pop(request_id, None)
        if isinstance(outcome, str):
            raise ScopeDenied(outcome)
        return outcome

    @classmethod
    def resolve(cls, request_id: str, grant_token: str) -> bool:
        fut = cls._waiters.get(request_id)
        if fut is None or fut.done():
            return False
        try:
            grant = verify_grant(grant_token)
        except GrantInvalid as exc:
            fut.set_result(f"invalid grant returned by platform: {exc}")
            return True
        fut.set_result((grant, grant_token))
        return True

    @classmethod
    def deny(cls, request_id: str, reason: str) -> bool:
        fut = cls._waiters.get(request_id)
        if fut is None or fut.done():
            return False
        fut.set_result(reason)
        return True


def _workspace_backend_artifacts_root(workspace: Any) -> str:
    write_prefixes = tuple(getattr(workspace, "write_prefixes", ()) or ())
    if not write_prefixes:
        write_prefixes = _normalize_write_prefixes(
            getattr(workspace, "outputs_prefix", None),
        )
    prefix = (write_prefixes[0] if write_prefixes else "outputs/").strip("/")
    return f"/{prefix}/.a2a-artifacts"


def _workspace_mount_alias(path: str | None) -> str | None:
    if path is None:
        return None
    clean = path.replace("\\", "/")
    if clean == "/workspace":
        return "/"
    if clean.startswith("/workspace/"):
        return clean[len("/workspace") :]
    return path


class _WorkspaceMountCompositeAdapter:
    """Normalize sandbox mount paths before DeepAgents' composite remaps output."""

    def __init__(self, backend: Any) -> None:
        self._backend = backend

    def __getattr__(self, name: str) -> Any:
        return getattr(self._backend, name)

    def read(self, file_path: str, *args: Any, **kwargs: Any) -> Any:
        return self._backend.read(_workspace_mount_alias(file_path), *args, **kwargs)

    async def aread(self, file_path: str, *args: Any, **kwargs: Any) -> Any:
        return await self._backend.aread(_workspace_mount_alias(file_path), *args, **kwargs)

    def write(self, file_path: str, content: str) -> Any:
        return self._backend.write(_workspace_mount_alias(file_path), content)

    async def awrite(self, file_path: str, content: str) -> Any:
        return await self._backend.awrite(_workspace_mount_alias(file_path), content)

    def edit(self, file_path: str, *args: Any, **kwargs: Any) -> Any:
        return self._backend.edit(_workspace_mount_alias(file_path), *args, **kwargs)

    async def aedit(self, file_path: str, *args: Any, **kwargs: Any) -> Any:
        return await self._backend.aedit(_workspace_mount_alias(file_path), *args, **kwargs)

    def ls(self, path: str) -> Any:
        return self._backend.ls(_workspace_mount_alias(path))

    async def als(self, path: str) -> Any:
        return await self._backend.als(_workspace_mount_alias(path))

    def glob(self, pattern: str, path: str = "/") -> Any:
        return self._backend.glob(pattern, _workspace_mount_alias(path))

    async def aglob(self, pattern: str, path: str = "/") -> Any:
        return await self._backend.aglob(pattern, _workspace_mount_alias(path))

    def grep(self, pattern: str, path: str | None = None, **kwargs: Any) -> Any:
        return self._backend.grep(pattern, _workspace_mount_alias(path), **kwargs)

    async def agrep(self, pattern: str, path: str | None = None, **kwargs: Any) -> Any:
        return await self._backend.agrep(pattern, _workspace_mount_alias(path), **kwargs)

    def download_files(self, paths: list[str]) -> Any:
        return self._backend.download_files(
            [_workspace_mount_alias(path) or "/" for path in paths]
        )

    async def adownload_files(self, paths: list[str]) -> Any:
        return await self._backend.adownload_files(
            [_workspace_mount_alias(path) or "/" for path in paths]
        )

    def upload_files(self, files: list[tuple[str, bytes]]) -> Any:
        return self._backend.upload_files(
            [(_workspace_mount_alias(path) or "/", content) for path, content in files]
        )

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> Any:
        return await self._backend.aupload_files(
            [(_workspace_mount_alias(path) or "/", content) for path, content in files]
        )


class RunContext(ABC, Generic[AuthT]):
    """Per-invocation context.

    A new context is constructed by the runtime for every skill call. It
    carries caller identity (``auth``), the task identity, and runtime
    capabilities (artifacts, secrets, streaming, cancellation).

    Agents must depend only on this abstract interface, never on a concrete
    runtime implementation.
    """

    task_id: str
    auth: AuthT
    caller: str = ""
    grant_ids: tuple[str, ...] = ()
    random_seed: str = ""

    def random(self) -> "random.Random":
        """A :class:`random.Random` seeded from ``self.random_seed``.

        Skill code should reach for this instead of :func:`random.random` or
        :func:`secrets.choice` when generating values it would like a
        deterministic replay to reproduce. The instance is cached on
        ``self._random`` so repeat calls in the same skill see the same RNG
        stream — and so a replay re-execution observes the same draws.
        """
        import random as _random

        cached = getattr(self, "_random", None)
        if cached is None:
            cached = _random.Random(self.random_seed or "")
            try:
                object.__setattr__(self, "_random", cached)
            except (AttributeError, TypeError):
                pass
        return cached

    @abstractmethod
    async def emit_event(self, event: AgentEvent) -> None:
        """Publish a structured event to subscribers (UI, logs, traces)."""

    @abstractmethod
    async def write_artifact(
        self, name: str, data: bytes, mime_type: str
    ) -> ArtifactRef:
        """Persist ``data`` as a named artifact and return a reference."""

    @abstractmethod
    async def check_cancelled(self) -> None:
        """Raise :class:`CancelledByCaller` if the caller cancelled."""

    @abstractmethod
    def secret(self, name: str) -> str:
        """Look up a runtime-injected secret by logical name."""

    def consumer_config(self, name: str, default: Any = None) -> Any:
        """Look up a caller-provided non-secret setup value."""
        values = getattr(self, "_consumer_config", None)
        if isinstance(values, dict) and name in values:
            return values[name]
        return default

    def consumer_secret(self, name: str) -> str:
        """Look up a caller-provided secret setup value."""
        values = getattr(self, "_consumer_secrets", None)
        if isinstance(values, dict) and name in values:
            return str(values[name])
        raise ConsumerSetupMissing(f"missing consumer setup secret: {name!r}")

    @property
    def llm(self) -> LLMCreds:
        """LLM endpoint + credentials for this invocation.

        Skill code that wants to make an LLM call should read this
        instead of hard-coding env-var lookups. The platform decides
        which creds to hand over based on the agent's
        :class:`LLMProvisioning` declaration + per-request data the
        control plane forwards (caller's keys, etc.).
        """
        import os

        forwarded = getattr(self, "_llm_creds", None)
        if isinstance(forwarded, LLMCreds):
            return forwarded
        # Agent-BYOK overrides — per-agent env vars take precedence over
        # local platform env so authors can swap providers without
        # rebuilding the CP.
        if os.environ.get("AGENT_LLM_KEY"):
            return LLMCreds(
                base_url=os.environ.get("AGENT_LLM_URL", "https://api.openai.com/v1"),
                api_key=os.environ["AGENT_LLM_KEY"],
                model=os.environ.get("AGENT_LLM_MODEL", "gpt-4o"),
                source="agent_byok",
            )
        return LLMCreds(
            base_url=(
                os.environ.get("A2A_LITELLM_URL",
                               "http://litellm.llm.svc.cluster.local:4000") + "/v1"
            ),
            api_key=os.environ.get("A2A_LITELLM_KEY", ""),
            model=os.environ.get("A2A_LITELLM_MODEL", "gpt-5.5"),
            source="platform",
        )

    def workspace_backend(self, *, image: str = "python:3.11-slim") -> Any:
        """Return a durable backend bound to this invocation workspace.

        Pass this to ``deepagents.create_deep_agent(..., backend=...)`` so
        DeepAgents' built-in file tools write to the caller's durable
        workspace instead of the default ephemeral LangGraph state backend.
        When the runtime attached ``ctx.sandbox``, the backend's ``execute``
        tool also runs in a sandbox with the same workspace mounted at
        ``/workspace``.
        """
        from .deepagents import WorkspaceBackend

        try:
            sandbox = self.sandbox
        except Exception:  # noqa: BLE001
            sandbox = None
        backend = WorkspaceBackend(
            self.workspace,
            sandbox=sandbox,
            default_image=image,
        )
        try:
            from deepagents.backends import CompositeBackend
        except Exception:  # pragma: no cover - deepagents optional
            return backend
        return _WorkspaceMountCompositeAdapter(CompositeBackend(
            default=backend,
            routes={},
            artifacts_root=_workspace_backend_artifacts_root(self.workspace),
        ))

    def deepagents_backend(self, *, image: str = "python:3.11-slim") -> Any:
        """Compatibility alias for :meth:`workspace_backend`."""
        return self.workspace_backend(image=image)

    async def workspace_shell(
        self,
        script: str,
        *,
        image: str = "python:3.11-slim",
        timeout_seconds: float | None = None,
        memory_mib: int = 512,
        cpus: int = 1,
    ) -> ExecResult:
        """Run shell code in a sandbox with this workspace mounted at ``/workspace``.

        Use this instead of in-process ``subprocess`` calls when the command
        creates files the caller should be able to download. Writes under
        ``/workspace`` persist directly. Other changed files in the sandbox
        root filesystem are captured under ``outputs/rootfs-captures/...``.
        """
        import uuid

        bucket = getattr(self.workspace, "bucket", None)
        spec = SandboxSpec(
            name=f"workspace-sh-{uuid.uuid4().hex[:8]}",
            image=image,
            workspace=bucket,
            memory_mib=memory_mib,
            cpus=cpus,
            labels={"workspace_write_policy": "workspace"},
        )
        sb = await self.sandbox.create(spec)
        try:
            return await sb.shell(script, timeout=timeout_seconds)
        finally:
            try:
                await sb.stop()
            except Exception:  # noqa: BLE001
                pass
            try:
                await self.sandbox.remove(getattr(sb, "name", spec.name))
            except Exception:  # noqa: BLE001
                pass

    async def workspace_python(
        self,
        code: str,
        *,
        image: str = "python:3.11-slim",
        timeout_seconds: float | None = None,
        memory_mib: int = 512,
        cpus: int = 1,
    ) -> ExecResult:
        """Run Python in a sandbox with this workspace mounted at ``/workspace``.

        Changed files outside ``/workspace`` are captured under
        ``outputs/rootfs-captures/...`` by the platform runtime.
        """
        import uuid

        bucket = getattr(self.workspace, "bucket", None)
        spec = SandboxSpec(
            name=f"workspace-py-{uuid.uuid4().hex[:8]}",
            image=image,
            workspace=bucket,
            memory_mib=memory_mib,
            cpus=cpus,
            labels={"workspace_write_policy": "workspace"},
        )
        sb = await self.sandbox.create(spec)
        try:
            return await sb.exec("python", ["-c", code], timeout=timeout_seconds)
        finally:
            try:
                await sb.stop()
            except Exception:  # noqa: BLE001
                pass
            try:
                await self.sandbox.remove(getattr(sb, "name", spec.name))
            except Exception:  # noqa: BLE001
                pass

    @property
    def cp_jwt(self) -> str | None:
        """Caller's control-plane JWT, when the platform forwarded it.

        Populated only if the agent class declares ``wants_cp_jwt=True``;
        otherwise always ``None``. Skills that need to call back into
        ``/v1/me/*`` on the user's behalf read this and pass it as the
        ``authorization: bearer ...`` header.
        """
        return getattr(self, "_cp_jwt", None)

    @property
    def cp_url(self) -> str | None:
        """Control-plane base URL paired with :attr:`cp_jwt`."""
        return getattr(self, "_cp_url", None)

    @property
    @abstractmethod
    def workspace(self) -> WorkspaceClient:
        """Negotiation surface for workspace access.

        Raises if the agent's :attr:`A2AAgent.workspace_access` is disabled.
        """

    @property
    @abstractmethod
    def sandbox(self) -> SandboxClient:
        """Code-execution surface (microsandbox-backed by default).

        Raises :class:`SandboxUnavailable` if the runtime did not attach a
        sandbox client to this context (e.g. local dev with no host daemon).
        """

    @property
    @abstractmethod
    def discover(self) -> DiscoveryClient:
        """Registry-backed discovery: find other agents by tag/capability/skill."""

    @property
    def subagents(self) -> Any:
        """Composable sub-agent toolkit over discovery, grants, and calls."""
        from .subagents import SubAgentToolkit

        return SubAgentToolkit(self)

    @property
    def memory(self) -> Any:
        """Workspace-file backed durable memory scoped to this invocation."""
        from .memory import MemoryClient

        return MemoryClient(self)

    @property
    def mail(self) -> Any:
        """Per-agent platform mailbox (IMAP/SMTP), or ``None``.

        Built lazily from the runtime-injected ``A2A_MAIL_*`` env vars and
        cached on the context. ``None`` when the control plane did not
        provision a mailbox for this agent.
        """
        if hasattr(self, "_mailbox"):
            return self._mailbox
        from .mail import AgentMailbox

        mailbox = AgentMailbox.from_env()
        try:
            object.__setattr__(self, "_mailbox", mailbox)
        except (AttributeError, TypeError):
            pass
        return mailbox

    @property
    def composition_budget(self) -> CompositionBudget:
        """Current recursive composition budget for this invocation tree."""

        current_agent = _current_agent_name(self)
        budget = ensure_composition_budget(
            getattr(self, "_composition_budget", None),
            current_agent=current_agent,
            llm_budget_usd=_grant_llm_budget(self),
        )
        try:
            object.__setattr__(self, "_composition_budget", budget)
        except (AttributeError, TypeError):
            pass
        return budget

    @property
    def meta_runs(self) -> Any:
        """Control-plane backed goal/plan/progress persistence."""
        from .meta_runs import MetaAgentRunsClient

        return MetaAgentRunsClient(self)

    @property
    def protocol_simulations(self) -> Any:
        """Control-plane backed protocol simulation scenario helpers."""
        from .protocol_simulations import ProtocolSimulationsClient

        return ProtocolSimulationsClient(self)

    async def call(
        self,
        target: str,
        skill: str,
        *,
        args: dict[str, Any] | None = None,
        grant: str | None = None,
        timeout: float | None = None,
        target_name: str | None = None,
        llm_budget_usd: float | None = None,
        consumer_config: dict[str, Any] | None = None,
        consumer_secrets: dict[str, str] | None = None,
    ) -> CallResult:
        """Invoke another agent's skill via the runtime's :class:`A2AClient`.

        ``target`` is whatever the underlying client expects — an HTTP URL
        for :class:`HttpA2AClient`, an agent name for in-process routing.
        Pair with :meth:`WorkspaceClient.delegate` to hand a scoped
        workspace grant to the callee.
        """
        client = self._a2a_client()
        forwarded = getattr(self, "_llm_creds", None)
        llm_creds = None
        if isinstance(forwarded, LLMCreds):
            llm_creds = {
                "base_url": forwarded.base_url,
                "api_key": forwarded.api_key,
                "model": forwarded.model,
                "source": forwarded.source,
                "temperature_mode": forwarded.temperature_mode,
                "temperature": forwarded.temperature,
                "extra_body": forwarded.extra_body or {},
                "metadata": forwarded.metadata or {},
            }
        target_identity = target_name or target
        try:
            parent_budget, child_budget = self.composition_budget.for_child(
                target_identity,
                llm_budget_usd=llm_budget_usd,
            )
        except CompositionLimitExceeded as exc:
            await self.emit_event(
                AgentEvent(
                    kind="composition_limit",
                    payload={
                        "target": target_identity,
                        "skill": skill,
                        "message": str(exc),
                        "budget": self.composition_budget.to_payload(),
                    },
                )
            )
            raise
        try:
            object.__setattr__(self, "_composition_budget", parent_budget)
        except (AttributeError, TypeError):
            pass
        await self.emit_event(
            AgentEvent(
                kind="composition_call_started",
                payload={
                    "target": target_identity,
                    "skill": skill,
                    "budget": child_budget.to_payload(),
                },
            )
        )
        try:
            result = await client.call(
                target,
                skill,
                args=args,
                grant=grant,
                cp_jwt=self.cp_jwt,
                cp_url=self.cp_url,
                llm_creds=llm_creds,
                consumer_config=consumer_config,
                consumer_secrets=consumer_secrets,
                timeout=timeout,
                composition=child_budget.to_payload(),
            )
        except Exception as exc:
            await self.emit_event(
                AgentEvent(
                    kind="composition_call_error",
                    payload={
                        "target": target_identity,
                        "skill": skill,
                        "message": str(exc),
                        "budget": child_budget.to_payload(),
                    },
                )
            )
            raise
        await self.emit_event(
            AgentEvent(
                kind="composition_call_complete",
                payload={
                    "target": target_identity,
                    "skill": skill,
                    "grant_id": result.grant_id,
                    "budget": child_budget.to_payload(),
                },
            )
        )
        return result

    @abstractmethod
    def _a2a_client(self) -> A2AClient:
        """Return the runtime's outbound A2A client (or raise if absent)."""

    # --- concrete helpers built on emit_event ---

    async def emit_progress(self, message: str) -> None:
        """Emit a human-readable progress event."""
        await self.emit_event(AgentEvent(kind="progress", payload={"message": message}))

    async def ask(self, prompt: str, *, timeout: float = 180.0) -> str:
        """Pause the skill until the caller answers a free-text question.

        Emits an event with ``kind="question"``. The runtime is responsible
        for routing the answer back via :meth:`answer`. If no answer arrives
        within ``timeout`` seconds, raises :class:`asyncio.TimeoutError`.
        """
        return await _AskRegistry.ask(self, prompt, timeout=timeout)

    async def collect(
        self,
        schema: type[BaseModel] | dict[str, Any],
        *,
        title: str = "More information needed",
        reason: str = "",
        ui_schema: dict[str, Any] | None = None,
        timeout: float = 300.0,
    ) -> BaseModel | dict[str, Any]:
        """Pause the skill until the caller submits structured input.

        ``schema`` may be a Pydantic model class or a JSON Schema object.
        The runtime emits ``kind="input_request"``; hosts render the schema
        as a form and POST the submitted JSON back. When a Pydantic model is
        supplied, the response is validated and returned as that model.
        """
        if isinstance(schema, type) and issubclass(schema, BaseModel):
            json_schema = schema.model_json_schema()
            raw = await _InputRegistry.collect(
                self,
                title=title,
                reason=reason,
                schema=json_schema,
                ui_schema=ui_schema,
                timeout=timeout,
            )
            return schema.model_validate(raw)
        if not isinstance(schema, dict):
            raise TypeError("collect schema must be a BaseModel class or JSON Schema dict")
        raw = await _InputRegistry.collect(
            self,
            title=title,
            reason=reason,
            schema=schema,
            ui_schema=ui_schema,
            timeout=timeout,
        )
        return TypeAdapter(dict[str, Any]).validate_python(raw)

    @classmethod
    def answer(cls, question_id: str, answer: str) -> bool:
        """Resolve a pending :meth:`ask` from outside (e.g. an HTTP handler)."""
        return _AskRegistry.answer(question_id, answer)

    @classmethod
    def submit_input(cls, request_id: str, value: dict[str, Any]) -> bool:
        """Resolve a pending :meth:`collect` from an HTTP/runtime handler."""
        return _InputRegistry.submit(request_id, value)

    def _workspace_covers_read(self, patterns: Sequence[str]) -> bool:
        if not patterns:
            return True
        ws = getattr(self, "_workspace", None)
        if ws is None:
            return False
        allowed = tuple(getattr(ws, "allow_patterns", ()) or ())
        if not allowed:
            return False
        access = getattr(ws, "_access", None)
        denied = tuple(getattr(access, "deny_patterns", ()) or ())
        for pattern in patterns:
            if any(fnmatch.fnmatch(pattern, deny) for deny in denied):
                return False
            if not any(
                current == "**"
                or current == pattern
                or fnmatch.fnmatch(pattern, current)
                for current in allowed
            ):
                return False
        return True

    def _workspace_covers_write(self, write_prefixes: Sequence[str]) -> bool:
        normalized = _normalize_write_prefixes(None, write_prefixes)
        if not normalized:
            return True
        ws = getattr(self, "_workspace", None)
        if ws is None:
            return False
        if getattr(ws, "current_mode", None) == WorkspaceMode.READ_ONLY:
            return False
        current = tuple(getattr(ws, "write_prefixes", ()) or ())
        outputs_prefix = getattr(ws, "outputs_prefix", None)
        if not current and outputs_prefix:
            current = _normalize_write_prefixes(outputs_prefix)
        if current:
            return all(_matches_write_prefix(prefix, current) for prefix in normalized)
        is_writable = getattr(ws, "is_writable_output", None)
        return bool(
            is_writable is not None
            and all(is_writable(prefix.strip("/")) for prefix in normalized)
        )

    async def request_scope(
        self,
        *,
        reason: str,
        read: Sequence[str] = (),
        write_prefix: str | None = None,
        write_prefixes: Sequence[str] = (),
        ttl_seconds: int = 60,
        mode: str = "read_only",
        timeout: float = 60.0,
        approval_timeout: float | None = None,
    ) -> Grant:
        """Ask the platform for a scope expansion mid-invocation.

        Pauses execution until the platform replies. On approve, the new
        :class:`Grant` is verified, installed on ``ctx.workspace``, and
        returned. On deny, raises :class:`ScopeDenied`.

        The tool must opt in by declaring ``allow_scope_expansion=True`` on
        its ``@tool`` decorator — otherwise raises
        :class:`ScopeExpansionNotAllowed`.
        """
        if not getattr(self, "_scope_expansion_allowed", False):
            raise ScopeExpansionNotAllowed(
                "this tool did not opt in to scope expansion; "
                "set @a2a.tool(allow_scope_expansion=True) to enable"
            )
        if not reason.strip():
            raise ValueError("request_scope: reason is required")
        grant, grant_token = await _ScopeRegistry.request(
            self,
            reason=reason,
            read=read,
            write_prefix=write_prefix,
            write_prefixes=write_prefixes,
            ttl_seconds=ttl_seconds,
            mode=mode,
            timeout=timeout,
            approval_timeout=approval_timeout,
        )
        # Install the grant on whatever workspace this context has, if any.
        ws = getattr(self, "_workspace", None)
        if ws is not None and hasattr(ws, "install_grant"):
            try:
                if hasattr(ws, "install_grant_token"):
                    ws.install_grant_token(grant, grant_token)
                else:
                    ws.install_grant(grant)
            except NotImplementedError:
                # Workspace doesn't support live grant install; the skill is
                # still free to use the returned grant value directly.
                pass
        return grant

    async def ensure_read(
        self,
        *,
        reason: str,
        patterns: Sequence[str],
        ttl_seconds: int = 60,
        timeout: float = 60.0,
        approval_timeout: float | None = None,
    ) -> Grant | None:
        """Request read scope only when the current grant does not cover it."""
        if self._workspace_covers_read(patterns):
            return None
        return await self.request_scope(
            reason=reason,
            read=patterns,
            ttl_seconds=ttl_seconds,
            mode="read_only",
            timeout=timeout,
            approval_timeout=approval_timeout,
        )

    async def ensure_write(
        self,
        *,
        reason: str,
        prefix: str | None = None,
        prefixes: Sequence[str] = (),
        ttl_seconds: int = 60,
        mode: str = "read_write_overlay",
        timeout: float = 60.0,
        approval_timeout: float | None = None,
    ) -> Grant | None:
        """Request write scope only when the current grant lacks a prefix."""
        requested = ((prefix,) if prefix else ()) + tuple(prefixes)
        write_prefixes = tuple(p for p in requested if p)
        if self._workspace_covers_write(write_prefixes):
            return None
        return await self.request_scope(
            reason=reason,
            write_prefixes=write_prefixes,
            ttl_seconds=ttl_seconds,
            mode=mode,
            timeout=timeout,
            approval_timeout=approval_timeout,
        )

    async def ensure_workspace(
        self,
        *,
        reason: str,
        read: Sequence[str] = (),
        write_prefix: str | None = None,
        write_prefixes: Sequence[str] = (),
        ttl_seconds: int = 60,
        mode: str = "read_only",
        timeout: float = 60.0,
        approval_timeout: float | None = None,
    ) -> Grant | None:
        """Request read/write scope only when the current grant is insufficient."""
        requested_write_prefixes = _normalize_write_prefixes(
            write_prefix,
            write_prefixes,
        )
        if self._workspace_covers_read(read) and self._workspace_covers_write(
            requested_write_prefixes
        ):
            return None
        return await self.request_scope(
            reason=reason,
            read=read,
            write_prefix=write_prefix,
            write_prefixes=write_prefixes,
            ttl_seconds=ttl_seconds,
            mode=mode,
            timeout=timeout,
            approval_timeout=approval_timeout,
        )

    @classmethod
    def resolve_scope_grant(cls, request_id: str, grant_token: str) -> bool:
        """Resolve a pending :meth:`request_scope` with a fresh signed grant.

        Called by the runtime adapter when the platform POSTs the new grant
        back to the agent (see ``serve/asgi.py``'s ``/scope-grants/{id}``).
        """
        return _ScopeRegistry.resolve(request_id, grant_token)

    @classmethod
    def deny_scope(cls, request_id: str, reason: str) -> bool:
        """Resolve a pending :meth:`request_scope` with a deny reason."""
        return _ScopeRegistry.deny(request_id, reason)

    async def emit_text_delta(self, text: str) -> None:
        """Emit a streamed token chunk (for LLM-style streaming output)."""
        await self.emit_event(AgentEvent(kind="text_delta", payload={"text": text}))

    async def emit_artifact(self, ref: ArtifactRef) -> None:
        """Notify subscribers that a new artifact is available."""
        await self.emit_event(
            AgentEvent(
                kind="artifact",
                payload={
                    "name": ref.name,
                    "uri": ref.uri,
                    "mime_type": ref.mime_type,
                    "size_bytes": ref.size_bytes,
                },
            )
        )

    async def emit_error(self, message: str, *, code: str | None = None) -> None:
        """Emit a structured error event (does not raise)."""
        await self.emit_event(
            AgentEvent(kind="error", payload={"message": message, "code": code})
        )

    async def mint_gitea_token(
        self,
        repo: str,
        *,
        scope: str = "read",
        ttl_seconds: int = 900,
        owner: str | None = None,
        purpose: str | None = None,
    ) -> dict[str, Any]:
        """Ask the control plane for a scoped Gitea token.

        Meta-agents (reviewer, patcher, migrator, composer) use this to
        construct a :class:`GiteaBackend` that reads or writes the source of
        another deployed agent. The returned token is minted as a dedicated
        read-only or writer service user — never the Gitea admin — and is
        bounded to a single ``(owner, repo)``.

        Returns a dict with keys ``token``, ``token_name``, ``username``,
        ``scopes``, ``expires_at``, ``repo``, ``owner``. Pass ``token_name``
        to :meth:`release_gitea_token` when finished.

        Raises ``RuntimeError`` if ``cp_jwt``/``cp_url`` are unset (the
        runtime did not forward control-plane credentials) or if the
        control plane refuses the mint.
        """
        if scope not in ("read", "write"):
            raise ValueError("scope must be 'read' or 'write'")
        if not self.cp_jwt or not self.cp_url:
            raise RuntimeError(
                "mint_gitea_token requires cp_jwt and cp_url; "
                "declare wants_cp_jwt=True on the agent"
            )
        import httpx  # late import: keeps client deps optional

        url = f"{self.cp_url.rstrip('/')}/v1/platform/gitea-token"
        body: dict[str, Any] = {
            "repo": repo,
            "scope": scope,
            "ttl_seconds": ttl_seconds,
        }
        if owner is not None:
            body["owner"] = owner
        if purpose is not None:
            body["purpose"] = purpose
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                json=body,
                headers={"authorization": f"bearer {self.cp_jwt}"},
            )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"gitea token mint failed: {resp.status_code}: {resp.text[:300]}"
            )
        return resp.json()

    async def release_gitea_token(self, token_name: str) -> None:
        """Revoke a Gitea token minted via :meth:`mint_gitea_token`.

        Safe to call from a ``finally`` block — the endpoint is idempotent
        and returns 204 even if the token has already been revoked or its
        TTL has elapsed. Callers that forget to release rely on the
        background sweeper to clean up.
        """
        if not self.cp_jwt or not self.cp_url:
            raise RuntimeError(
                "release_gitea_token requires cp_jwt and cp_url; "
                "declare wants_cp_jwt=True on the agent"
            )
        import httpx

        url = f"{self.cp_url.rstrip('/')}/v1/platform/gitea-token/{token_name}"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.delete(
                url,
                headers={"authorization": f"bearer {self.cp_jwt}"},
            )
        if resp.status_code >= 400 and resp.status_code != 404:
            raise RuntimeError(
                f"gitea token release failed: {resp.status_code}: {resp.text[:300]}"
            )

    def require_scopes(self, required: Sequence[str]) -> None:
        """Raise :class:`MissingScopes` if ``self.auth`` lacks any required scope.

        Auth models without a ``scopes`` attribute (e.g. :class:`NoAuth`) are
        treated as having an empty scope set.
        """
        if not required:
            return
        auth_scopes = set(getattr(self.auth, "scopes", ()) or ())
        missing = [s for s in required if s not in auth_scopes]
        if missing:
            raise MissingScopes(missing)


def _current_agent_name(ctx: RunContext[Any]) -> str:
    name = getattr(ctx, "_current_agent_name", None)
    if isinstance(name, str) and name.strip():
        return name
    workspace = getattr(ctx, "_workspace", None)
    issuer = getattr(workspace, "issuer", None)
    if isinstance(issuer, str) and issuer.strip():
        return issuer
    caller = getattr(ctx, "caller", None)
    if isinstance(caller, str) and caller.strip():
        return caller
    return "caller"


def _grant_llm_budget(ctx: RunContext[Any]) -> float | None:
    workspace = getattr(ctx, "_workspace", None)
    grant = getattr(workspace, "current_grant", None)
    value = getattr(grant, "llm_max_budget_usd", None)
    return float(value) if value is not None else None


class LocalRunContext(RunContext[AuthT]):
    """In-memory context for local dev and tests.

    Stores events and artifacts in lists/dicts. Secrets come from a plain
    mapping. Cancellation is driven by an :class:`asyncio.Event`.
    """

    def __init__(
        self,
        *,
        auth: AuthT,
        task_id: str = "local-task",
        secrets: dict[str, str] | None = None,
        workspace: WorkspaceClient | None = None,
        sandbox: SandboxClient | None = None,
        a2a: A2AClient | None = None,
        discover: DiscoveryClient | None = None,
        consumer_config: dict[str, Any] | None = None,
        consumer_secrets: dict[str, str] | None = None,
        on_event: Any | None = None,  # async (AgentEvent) -> None
        caller: str = "",
        grant_ids: tuple[str, ...] = (),
        random_seed: str | None = None,
        composition_budget: CompositionBudget | dict[str, Any] | None = None,
    ) -> None:
        import secrets as _secrets

        self.task_id = task_id
        self.auth = auth
        self.caller = caller
        self.grant_ids = tuple(grant_ids)
        self.random_seed = (
            random_seed if random_seed is not None else _secrets.token_hex(16)
        )
        self._secrets: dict[str, str] = dict(secrets or {})
        self._workspace = workspace
        self._sandbox = sandbox
        self._a2a = a2a
        self._discover = discover
        self._consumer_config = dict(consumer_config or {})
        self._consumer_secrets = dict(consumer_secrets or {})
        self._cancel = asyncio.Event()
        self.events: list[AgentEvent] = []
        self.artifacts: dict[str, bytes] = {}
        self._on_event = on_event
        if composition_budget is not None:
            self._composition_budget = ensure_composition_budget(
                composition_budget,
                current_agent=caller or "caller",
                llm_budget_usd=_grant_llm_budget(self),
            )

    @property
    def workspace(self) -> WorkspaceClient:
        if self._workspace is None:
            raise PermissionError(
                "no workspace bound to this context; agent did not declare "
                "workspace_access or runtime did not provision one"
            )
        return self._workspace

    @property
    def sandbox(self) -> SandboxClient:
        if self._sandbox is None:
            raise SandboxUnavailable(
                "no sandbox client attached to this context; "
                "the runtime layer must provision one"
            )
        return self._sandbox

    @property
    def discover(self) -> DiscoveryClient:
        if self._discover is None:
            raise PermissionError(
                "no discovery client attached; runtime must provision one"
            )
        return self._discover

    def _a2a_client(self) -> A2AClient:
        if self._a2a is None:
            raise PermissionError(
                "no A2A client attached; runtime must provision one before "
                "ctx.call(...) can be used"
            )
        return self._a2a

    async def emit_event(self, event: AgentEvent) -> None:
        self.events.append(event)
        if self._on_event is not None:
            await self._on_event(event)

    async def write_artifact(
        self, name: str, data: bytes, mime_type: str
    ) -> ArtifactRef:
        self.artifacts[name] = data
        return ArtifactRef(
            name=name,
            uri=f"memory://{self.task_id}/{name}",
            mime_type=mime_type,
            size_bytes=len(data),
        )

    async def check_cancelled(self) -> None:
        if self._cancel.is_set():
            raise CancelledByCaller(self.task_id)

    def cancel(self) -> None:
        self._cancel.set()

    def secret(self, name: str) -> str:
        try:
            return self._secrets[name]
        except KeyError as exc:
            raise KeyError(f"unknown secret: {name!r}") from exc
