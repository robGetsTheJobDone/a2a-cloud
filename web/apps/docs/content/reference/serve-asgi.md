# `a2a_pack.serve.asgi`

HTTP adapter that turns any :class:`A2AAgent` into a service.

This is intentionally minimal: it covers the surface needed to plug into
the wider A2A ecosystem and the platform's control plane.

Endpoints:
  GET  /healthz                     -> liveness
  GET  /.well-known/agent-card      -> Agent Card JSON
  POST /invoke/{skill}              -> invoke skill (JSON in, JSON out)

Auth: by default a single bearer token is read from the ``A2A_API_KEY`` env
var. Agents can instead set ``auth_resolver`` to validate caller bearer tokens
against their own API, OIDC userinfo/introspection endpoint, or other identity
bridge. The resolved principal is materialized into the declared
``auth_model``.

## `build_app`  *(function)*

```python
build_app(agent: 'A2AAgent', *, frontend: 'PackedFrontend | None' = None) -> 'FastAPI'
```

Build a FastAPI app for the given agent instance.

## `queue_get_or_heartbeat`  *(function)*

```python
queue_get_or_heartbeat(queue: 'asyncio.Queue[dict[str, Any] | None]', timeout: 'float' = 15.0) -> 'dict[str, Any] | None | object'
```

*(no docstring)*

## `serve`  *(function)*

```python
serve(agent: 'A2AAgent', *, host: 'str' = '0.0.0.0', port: 'int' = 8000, frontend: 'PackedFrontend | None' = None) -> 'None'
```

Run the agent's HTTP server with uvicorn (blocking).

## `sse_comment`  *(function)*

```python
sse_comment(comment: 'str' = 'ping') -> 'bytes'
```

*(no docstring)*


*Source: `sdk/a2a-pack/a2a_pack/serve/asgi.py`*