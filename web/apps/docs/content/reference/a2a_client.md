# `a2a_pack.a2a_client`

Agent-to-agent invocation surface available via ``ctx.call(...)``.

An agent never speaks raw HTTP to another agent. It calls
``ctx.call(target, skill, args, grant=...)`` and the runtime-attached
:class:`A2AClient` handles transport: HTTP for cross-pod, in-memory for
local tests, anything else (gRPC, message bus) for future runtimes.

The grant token (see :mod:`a2a_pack.grants`) is the *only* way to hand
workspace access across agents. Callee-side runtime validates it before
materializing a :class:`WorkspaceClient`.

## `A2AClient`  *(class)*

```python
A2AClient()
```

Transport-shaped agent-to-agent client.

### `call`  *(method)*

```python
call(self, target: 'str', skill: 'str', *, args: 'dict[str, Any] | None' = None, grant: 'str | None' = None, cp_jwt: 'str | None' = None, cp_url: 'str | None' = None, llm_creds: 'dict[str, Any] | None' = None, consumer_config: 'dict[str, Any] | None' = None, consumer_secrets: 'dict[str, str] | None' = None, timeout: 'float | None' = None, composition: 'dict[str, Any] | None' = None) -> 'CallResult'
```

Invoke ``skill`` on ``target`` and return its :class:`CallResult`.

``target`` is opaque to this layer — for the HTTP impl it's an agent
URL; for the in-memory impl it's an agent name.

## `CallResult`  *(class)*

```python
CallResult(result: 'Any', events: 'tuple[dict[str, Any], ...]' = (), artifacts: 'tuple[dict[str, Any], ...]' = (), grant_id: 'str | None' = None) -> None
```

What an A2A invocation returns to the calling skill.

### `__init__`  *(method)*

```python
__init__(self, result: 'Any', events: 'tuple[dict[str, Any], ...]' = (), artifacts: 'tuple[dict[str, Any], ...]' = (), grant_id: 'str | None' = None) -> None
```

*(no docstring)*

## `HttpA2AClient`  *(class)*

```python
HttpA2AClient(default_timeout: 'float' = 60.0, discovery: 'Any | None' = None) -> None
```

A2A client that POSTs to the standard /invoke/{skill} endpoint.

``target`` may be a concrete URL or a platform registry name. Hosted
runtimes attach discovery so meta-agents can use stable agent names while
the transport still calls the live Agent Card URL.

### `__init__`  *(method)*

```python
__init__(self, default_timeout: 'float' = 60.0, discovery: 'Any | None' = None) -> None
```

*(no docstring)*

### `call`  *(method)*

```python
call(self, target: 'str', skill: 'str', *, args: 'dict[str, Any] | None' = None, grant: 'str | None' = None, cp_jwt: 'str | None' = None, cp_url: 'str | None' = None, llm_creds: 'dict[str, Any] | None' = None, consumer_config: 'dict[str, Any] | None' = None, consumer_secrets: 'dict[str, str] | None' = None, timeout: 'float | None' = None, composition: 'dict[str, Any] | None' = None) -> 'CallResult'
```

*(no docstring)*

## `InMemoryA2AClient`  *(class)*

```python
InMemoryA2AClient(agents: "dict[str, 'A2AAgent']", ctx_factory: 'Any' = None) -> None
```

Routes calls to agent instances registered by name.

The receiving agent gets a *new* :class:`RunContext` built by the
``ctx_factory`` callable, so caller and callee don't share state.
Pass ``ctx_factory=lambda agent, grant: ...`` to control how scoped
workspaces / sandboxes are wired in.

### `__init__`  *(method)*

```python
__init__(self, agents: "dict[str, 'A2AAgent']", ctx_factory: 'Any' = None) -> None
```

*(no docstring)*

### `call`  *(method)*

```python
call(self, target: 'str', skill: 'str', *, args: 'dict[str, Any] | None' = None, grant: 'str | None' = None, cp_jwt: 'str | None' = None, cp_url: 'str | None' = None, llm_creds: 'dict[str, Any] | None' = None, consumer_config: 'dict[str, Any] | None' = None, consumer_secrets: 'dict[str, str] | None' = None, timeout: 'float | None' = None, composition: 'dict[str, Any] | None' = None) -> 'CallResult'
```

*(no docstring)*


*Source: `sdk/a2a-pack/a2a_pack/a2a_client.py`*