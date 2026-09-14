# `a2a_pack.agent`



## `A2AAgent`  *(class)*

```python
A2AAgent(config: 'ConfigT | dict[str, Any] | None' = None) -> 'None'
```

Base class for A2A agents.

Subclasses declare:

- ``name``, ``description`` (and optional ``version``),
- optional ``config_model`` / ``auth_model`` (default to empty / NoAuth),
- deployment metadata: ``required_secrets``, ``required_env``,
  ``capabilities``, ``input_modes``, ``output_modes``,
- one or more methods decorated with :func:`skill`.

### `__init__`  *(method)*

```python
__init__(self, config: 'ConfigT | dict[str, Any] | None' = None) -> 'None'
```

*(no docstring)*

### `card`  *(method)*

```python
card(self) -> 'AgentCard'
```

*(no docstring)*

### `health`  *(method)*

```python
health(self) -> 'bool'
```

Lightweight liveness check. Override to add real probes.

### `invoke`  *(method)*

```python
invoke(self, skill_name: 'str', ctx: 'RunContext[AuthT]', /, **kwargs: 'Any') -> 'Any'
```

Invoke a skill with caller-supplied kwargs.

Inputs are validated and coerced via the skill's pydantic schema.
Required scopes are enforced against ``ctx.auth`` before the handler
runs. The raw handler return value is returned (Python-typed).

### `invoke_json`  *(method)*

```python
invoke_json(self, skill_name: 'str', ctx: 'RunContext[AuthT]', payload: 'dict[str, Any]') -> 'Any'
```

Runtime-facing invoke: takes JSON-shaped payload, returns JSON-shaped result.

### `local_invoke`  *(method)*

```python
local_invoke(self, skill_name: 'str', /, *, auth: 'AuthT | None' = None, secrets: 'dict[str, str] | None' = None, task_id: 'str' = 'local-task', workspace: 'Any' = None, sandbox: 'Any' = None, a2a: 'Any' = None, discover: 'Any' = None, consumer_config: 'dict[str, Any] | None' = None, consumer_secrets: 'dict[str, str] | None' = None, **kwargs: 'Any') -> 'Any'
```

Convenience harness: build a :class:`LocalRunContext` and invoke.

Useful in tests and notebooks. Pass ``workspace=``, ``sandbox=``,
``a2a=``, and/or ``discover=`` to bind concrete runtime clients.

### `runtime`  *(method)*

```python
runtime() -> 'AgentRuntime'
```

Aggregate the class-level runtime declaration.

``sandbox`` is always :attr:`Sandbox.MICROSANDBOX`; it is set here
rather than on the class so developers cannot weaken isolation.

### `shutdown`  *(method)*

```python
shutdown(self, ctx: 'RunContext[AuthT]') -> 'None'
```

Called once before the agent process exits. Override to tear down.

### `startup`  *(method)*

```python
startup(self, ctx: 'RunContext[AuthT]') -> 'None'
```

Called once before the first invocation. Override to set up state.

## `ParamSpec`  *(class)*

```python
ParamSpec(name: 'str', adapter: 'TypeAdapter[Any]', has_default: 'bool', default: 'Any' = None) -> None
```

Validation metadata for a single skill parameter.

### `__init__`  *(method)*

```python
__init__(self, name: 'str', adapter: 'TypeAdapter[Any]', has_default: 'bool', default: 'Any' = None) -> None
```

*(no docstring)*

## `SkillInputError`  *(class)*



Raised when invoke() inputs fail validation against the skill schema.

## `SkillInvocationError`  *(class)*



Raised when a skill handler raises during invoke().

## `SkillNotFound`  *(class)*



Raised when invoke() is called with an unknown skill name.

## `SkillSpec`  *(class)*

```python
SkillSpec(name: 'str', description: 'str', tags: 'tuple[str, ...]', scopes: 'tuple[str, ...]', stream: 'bool', policy: 'SkillPolicy', input_schema: 'dict[str, Any]', output_schema: 'dict[str, Any]', handler: 'Callable[..., Awaitable[Any]]', params: 'tuple[ParamSpec, ...]' = <factory>, output_adapter: 'TypeAdapter[Any] | None' = None) -> None
```

Static metadata about a single skill, captured at decoration time.

### `__init__`  *(method)*

```python
__init__(self, name: 'str', description: 'str', tags: 'tuple[str, ...]', scopes: 'tuple[str, ...]', stream: 'bool', policy: 'SkillPolicy', input_schema: 'dict[str, Any]', output_schema: 'dict[str, Any]', handler: 'Callable[..., Awaitable[Any]]', params: 'tuple[ParamSpec, ...]' = <factory>, output_adapter: 'TypeAdapter[Any] | None' = None) -> None
```

*(no docstring)*

## `skill`  *(function)*

```python
skill(*, name: 'str | None' = None, description: 'str' = '', tags: 'Sequence[str]' = (), scopes: 'Sequence[str]' = (), stream: 'bool' = False, on_email: 'bool' = False, input_schema: 'dict[str, Any] | None' = None, timeout_seconds: 'float | None' = None, idempotent: 'bool' = False, max_retries: 'int' = 0, cost_class: 'str | None' = None, allow_scope_expansion: 'bool' = False, grant_mode: 'str | None' = None, grant_allow_patterns: 'Sequence[str]' = (), grant_deny_patterns: 'Sequence[str]' = (), grant_outputs_prefix: 'str | None' = None, grant_write_prefixes: 'Sequence[str]' = (), grant_ttl_seconds: 'int | None' = None, grant_run_timeout_seconds: 'int | None' = None, grant_approval_timeout_seconds: 'int | None' = None, grant_scope_approval_timeout_seconds: 'int | None' = None) -> 'Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]'
```

Mark an :class:`A2AAgent` method as a callable, typed tool.

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

## `tool`  *(function)*

```python
tool(*, name: 'str | None' = None, description: 'str' = '', tags: 'Sequence[str]' = (), scopes: 'Sequence[str]' = (), stream: 'bool' = False, on_email: 'bool' = False, input_schema: 'dict[str, Any] | None' = None, timeout_seconds: 'float | None' = None, idempotent: 'bool' = False, max_retries: 'int' = 0, cost_class: 'str | None' = None, allow_scope_expansion: 'bool' = False, grant_mode: 'str | None' = None, grant_allow_patterns: 'Sequence[str]' = (), grant_deny_patterns: 'Sequence[str]' = (), grant_outputs_prefix: 'str | None' = None, grant_write_prefixes: 'Sequence[str]' = (), grant_ttl_seconds: 'int | None' = None, grant_run_timeout_seconds: 'int | None' = None, grant_approval_timeout_seconds: 'int | None' = None, grant_scope_approval_timeout_seconds: 'int | None' = None) -> 'Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]'
```

Mark an :class:`A2AAgent` method as a callable, typed tool.

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


*Source: `sdk/a2a-pack/a2a_pack/agent.py`*