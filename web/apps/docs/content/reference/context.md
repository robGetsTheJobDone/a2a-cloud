# `a2a_pack.context`

Runtime context handed to skill handlers.

The same agent code runs unchanged on local dev, Docker, Kubernetes, and
hosted runtimes — the runtime provides a concrete :class:`RunContext` that
implements artifact storage, secret access, streaming, and cancellation.

## `AgentEvent`  *(class)*

```python
AgentEvent(kind: 'str', payload: 'dict[str, Any]' = <factory>) -> None
```

A structured event emitted during a skill run.

### `__init__`  *(method)*

```python
__init__(self, kind: 'str', payload: 'dict[str, Any]' = <factory>) -> None
```

*(no docstring)*

## `ArtifactRef`  *(class)*

```python
ArtifactRef(name: 'str', uri: 'str', mime_type: 'str', size_bytes: 'int') -> None
```

Opaque handle to a stored artifact (blob, file, etc.).

### `__init__`  *(method)*

```python
__init__(self, name: 'str', uri: 'str', mime_type: 'str', size_bytes: 'int') -> None
```

*(no docstring)*

## `CancelledByCaller`  *(class)*



Raised by :meth:`RunContext.check_cancelled` when the caller cancelled.

## `ConsumerSetupMissing`  *(class)*



Raised when a skill reads a caller setup value that was not provided.

## `LLMCreds`  *(class)*

```python
LLMCreds(base_url: 'str', api_key: 'str', model: 'str', source: 'str', temperature_mode: 'str' = 'default', temperature: 'float | None' = None, extra_body: 'dict[str, Any] | None' = None, metadata: 'dict[str, Any] | None' = None) -> None
```

An LLM endpoint + credentials handed to the skill at runtime.

Resolution order — checked in :meth:`RunContext.llm`:

  1. ``llm_creds`` in the inbound /invoke body — set by the control
     plane from the caller's saved LLM credential.
  2. Per-agent env vars: ``AGENT_LLM_{URL,KEY,MODEL}`` for
     ``llm_provisioning=agent_byok``.
  3. Local platform env: ``A2A_LITELLM_{URL,KEY,MODEL}`` for trusted
     platform-owned services and local development.

Skill code shouldn't care which branch fired — just read
``ctx.llm`` and pass it through to your chat client.

### `__init__`  *(method)*

```python
__init__(self, base_url: 'str', api_key: 'str', model: 'str', source: 'str', temperature_mode: 'str' = 'default', temperature: 'float | None' = None, extra_body: 'dict[str, Any] | None' = None, metadata: 'dict[str, Any] | None' = None) -> None
```

*(no docstring)*

## `LocalRunContext`  *(class)*

```python
LocalRunContext(*, auth: 'AuthT', task_id: 'str' = 'local-task', secrets: 'dict[str, str] | None' = None, workspace: 'WorkspaceClient | None' = None, sandbox: 'SandboxClient | None' = None, a2a: 'A2AClient | None' = None, discover: 'DiscoveryClient | None' = None, consumer_config: 'dict[str, Any] | None' = None, consumer_secrets: 'dict[str, str] | None' = None, on_event: 'Any | None' = None, caller: 'str' = '', grant_ids: 'tuple[str, ...]' = (), random_seed: 'str | None' = None, composition_budget: 'CompositionBudget | dict[str, Any] | None' = None) -> 'None'
```

In-memory context for local dev and tests.

Stores events and artifacts in lists/dicts. Secrets come from a plain
mapping. Cancellation is driven by an :class:`asyncio.Event`.

### `__init__`  *(method)*

```python
__init__(self, *, auth: 'AuthT', task_id: 'str' = 'local-task', secrets: 'dict[str, str] | None' = None, workspace: 'WorkspaceClient | None' = None, sandbox: 'SandboxClient | None' = None, a2a: 'A2AClient | None' = None, discover: 'DiscoveryClient | None' = None, consumer_config: 'dict[str, Any] | None' = None, consumer_secrets: 'dict[str, str] | None' = None, on_event: 'Any | None' = None, caller: 'str' = '', grant_ids: 'tuple[str, ...]' = (), random_seed: 'str | None' = None, composition_budget: 'CompositionBudget | dict[str, Any] | None' = None) -> 'None'
```

*(no docstring)*

### `cancel`  *(method)*

```python
cancel(self) -> 'None'
```

*(no docstring)*

### `check_cancelled`  *(method)*

```python
check_cancelled(self) -> 'None'
```

*(no docstring)*

### `emit_event`  *(method)*

```python
emit_event(self, event: 'AgentEvent') -> 'None'
```

*(no docstring)*

### `secret`  *(method)*

```python
secret(self, name: 'str') -> 'str'
```

*(no docstring)*

### `write_artifact`  *(method)*

```python
write_artifact(self, name: 'str', data: 'bytes', mime_type: 'str') -> 'ArtifactRef'
```

*(no docstring)*

## `MissingScopes`  *(class)*

```python
MissingScopes(missing: 'Sequence[str]') -> 'None'
```

Raised by :meth:`RunContext.require_scopes` when caller lacks scopes.

### `__init__`  *(method)*

```python
__init__(self, missing: 'Sequence[str]') -> 'None'
```

*(no docstring)*

## `RunContext`  *(class)*

```python
RunContext()
```

Per-invocation context.

A new context is constructed by the runtime for every skill call. It
carries caller identity (``auth``), the task identity, and runtime
capabilities (artifacts, secrets, streaming, cancellation).

Agents must depend only on this abstract interface, never on a concrete
runtime implementation.

### `answer`  *(method)*

```python
answer(question_id: 'str', answer: 'str') -> 'bool'
```

Resolve a pending :meth:`ask` from outside (e.g. an HTTP handler).

### `ask`  *(method)*

```python
ask(self, prompt: 'str', *, timeout: 'float' = 180.0) -> 'str'
```

Pause the skill until the caller answers a free-text question.

Emits an event with ``kind="question"``. The runtime is responsible
for routing the answer back via :meth:`answer`. If no answer arrives
within ``timeout`` seconds, raises :class:`asyncio.TimeoutError`.

### `call`  *(method)*

```python
call(self, target: 'str', skill: 'str', *, args: 'dict[str, Any] | None' = None, grant: 'str | None' = None, timeout: 'float | None' = None, target_name: 'str | None' = None, llm_budget_usd: 'float | None' = None, consumer_config: 'dict[str, Any] | None' = None, consumer_secrets: 'dict[str, str] | None' = None) -> 'CallResult'
```

Invoke another agent's skill via the runtime's :class:`A2AClient`.

``target`` is whatever the underlying client expects — an HTTP URL
for :class:`HttpA2AClient`, an agent name for in-process routing.
Pair with :meth:`WorkspaceClient.delegate` to hand a scoped
workspace grant to the callee.

### `check_cancelled`  *(method)*

```python
check_cancelled(self) -> 'None'
```

Raise :class:`CancelledByCaller` if the caller cancelled.

### `collect`  *(method)*

```python
collect(self, schema: 'type[BaseModel] | dict[str, Any]', *, title: 'str' = 'More information needed', reason: 'str' = '', ui_schema: 'dict[str, Any] | None' = None, timeout: 'float' = 300.0) -> 'BaseModel | dict[str, Any]'
```

Pause the skill until the caller submits structured input.

``schema`` may be a Pydantic model class or a JSON Schema object.
The runtime emits ``kind="input_request"``; hosts render the schema
as a form and POST the submitted JSON back. When a Pydantic model is
supplied, the response is validated and returned as that model.

### `consumer_config`  *(method)*

```python
consumer_config(self, name: 'str', default: 'Any' = None) -> 'Any'
```

Look up a caller-provided non-secret setup value.

### `consumer_secret`  *(method)*

```python
consumer_secret(self, name: 'str') -> 'str'
```

Look up a caller-provided secret setup value.

### `deepagents_backend`  *(method)*

```python
deepagents_backend(self, *, image: 'str' = 'python:3.11-slim') -> 'Any'
```

Compatibility alias for :meth:`workspace_backend`.

### `deny_scope`  *(method)*

```python
deny_scope(request_id: 'str', reason: 'str') -> 'bool'
```

Resolve a pending :meth:`request_scope` with a deny reason.

### `emit_artifact`  *(method)*

```python
emit_artifact(self, ref: 'ArtifactRef') -> 'None'
```

Notify subscribers that a new artifact is available.

### `emit_error`  *(method)*

```python
emit_error(self, message: 'str', *, code: 'str | None' = None) -> 'None'
```

Emit a structured error event (does not raise).

### `emit_event`  *(method)*

```python
emit_event(self, event: 'AgentEvent') -> 'None'
```

Publish a structured event to subscribers (UI, logs, traces).

### `emit_progress`  *(method)*

```python
emit_progress(self, message: 'str') -> 'None'
```

Emit a human-readable progress event.

### `emit_text_delta`  *(method)*

```python
emit_text_delta(self, text: 'str') -> 'None'
```

Emit a streamed token chunk (for LLM-style streaming output).

### `ensure_read`  *(method)*

```python
ensure_read(self, *, reason: 'str', patterns: 'Sequence[str]', ttl_seconds: 'int' = 60, timeout: 'float' = 60.0, approval_timeout: 'float | None' = None) -> 'Grant | None'
```

Request read scope only when the current grant does not cover it.

### `ensure_workspace`  *(method)*

```python
ensure_workspace(self, *, reason: 'str', read: 'Sequence[str]' = (), write_prefix: 'str | None' = None, write_prefixes: 'Sequence[str]' = (), ttl_seconds: 'int' = 60, mode: 'str' = 'read_only', timeout: 'float' = 60.0, approval_timeout: 'float | None' = None) -> 'Grant | None'
```

Request read/write scope only when the current grant is insufficient.

### `ensure_write`  *(method)*

```python
ensure_write(self, *, reason: 'str', prefix: 'str | None' = None, prefixes: 'Sequence[str]' = (), ttl_seconds: 'int' = 60, mode: 'str' = 'read_write_overlay', timeout: 'float' = 60.0, approval_timeout: 'float | None' = None) -> 'Grant | None'
```

Request write scope only when the current grant lacks a prefix.

### `mint_gitea_token`  *(method)*

```python
mint_gitea_token(self, repo: 'str', *, scope: 'str' = 'read', ttl_seconds: 'int' = 900, owner: 'str | None' = None, purpose: 'str | None' = None) -> 'dict[str, Any]'
```

Ask the control plane for a scoped Gitea token.

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

### `random`  *(method)*

```python
random(self) -> "'random.Random'"
```

A :class:`random.Random` seeded from ``self.random_seed``.

Skill code should reach for this instead of :func:`random.random` or
:func:`secrets.choice` when generating values it would like a
deterministic replay to reproduce. The instance is cached on
``self._random`` so repeat calls in the same skill see the same RNG
stream — and so a replay re-execution observes the same draws.

### `release_gitea_token`  *(method)*

```python
release_gitea_token(self, token_name: 'str') -> 'None'
```

Revoke a Gitea token minted via :meth:`mint_gitea_token`.

Safe to call from a ``finally`` block — the endpoint is idempotent
and returns 204 even if the token has already been revoked or its
TTL has elapsed. Callers that forget to release rely on the
background sweeper to clean up.

### `request_scope`  *(method)*

```python
request_scope(self, *, reason: 'str', read: 'Sequence[str]' = (), write_prefix: 'str | None' = None, write_prefixes: 'Sequence[str]' = (), ttl_seconds: 'int' = 60, mode: 'str' = 'read_only', timeout: 'float' = 60.0, approval_timeout: 'float | None' = None) -> 'Grant'
```

Ask the platform for a scope expansion mid-invocation.

Pauses execution until the platform replies. On approve, the new
:class:`Grant` is verified, installed on ``ctx.workspace``, and
returned. On deny, raises :class:`ScopeDenied`.

The tool must opt in by declaring ``allow_scope_expansion=True`` on
its ``@tool`` decorator — otherwise raises
:class:`ScopeExpansionNotAllowed`.

### `require_scopes`  *(method)*

```python
require_scopes(self, required: 'Sequence[str]') -> 'None'
```

Raise :class:`MissingScopes` if ``self.auth`` lacks any required scope.

Auth models without a ``scopes`` attribute (e.g. :class:`NoAuth`) are
treated as having an empty scope set.

### `resolve_scope_grant`  *(method)*

```python
resolve_scope_grant(request_id: 'str', grant_token: 'str') -> 'bool'
```

Resolve a pending :meth:`request_scope` with a fresh signed grant.

Called by the runtime adapter when the platform POSTs the new grant
back to the agent (see ``serve/asgi.py``'s ``/scope-grants/{id}``).

### `secret`  *(method)*

```python
secret(self, name: 'str') -> 'str'
```

Look up a runtime-injected secret by logical name.

### `submit_input`  *(method)*

```python
submit_input(request_id: 'str', value: 'dict[str, Any]') -> 'bool'
```

Resolve a pending :meth:`collect` from an HTTP/runtime handler.

### `workspace_backend`  *(method)*

```python
workspace_backend(self, *, image: 'str' = 'python:3.11-slim') -> 'Any'
```

Return a durable backend bound to this invocation workspace.

Pass this to ``deepagents.create_deep_agent(..., backend=...)`` so
DeepAgents' built-in file tools write to the caller's durable
workspace instead of the default ephemeral LangGraph state backend.
When the runtime attached ``ctx.sandbox``, the backend's ``execute``
tool also runs in a sandbox with the same workspace mounted at
``/workspace``.

### `workspace_python`  *(method)*

```python
workspace_python(self, code: 'str', *, image: 'str' = 'python:3.11-slim', timeout_seconds: 'float | None' = None, memory_mib: 'int' = 512, cpus: 'int' = 1) -> 'ExecResult'
```

Run Python in a sandbox with this workspace mounted at ``/workspace``.

Changed files outside ``/workspace`` are captured under
``outputs/rootfs-captures/...`` by the platform runtime.

### `workspace_shell`  *(method)*

```python
workspace_shell(self, script: 'str', *, image: 'str' = 'python:3.11-slim', timeout_seconds: 'float | None' = None, memory_mib: 'int' = 512, cpus: 'int' = 1) -> 'ExecResult'
```

Run shell code in a sandbox with this workspace mounted at ``/workspace``.

Use this instead of in-process ``subprocess`` calls when the command
creates files the caller should be able to download. Writes under
``/workspace`` persist directly. Other changed files in the sandbox
root filesystem are captured under ``outputs/rootfs-captures/...``.

### `write_artifact`  *(method)*

```python
write_artifact(self, name: 'str', data: 'bytes', mime_type: 'str') -> 'ArtifactRef'
```

Persist ``data`` as a named artifact and return a reference.

## `ScopeDenied`  *(class)*



Raised by :meth:`RunContext.request_scope` when the platform refuses.

## `ScopeExpansionNotAllowed`  *(class)*



Raised when a tool calls request_scope without opting in via @tool.


*Source: `sdk/a2a-pack/a2a_pack/context.py`*