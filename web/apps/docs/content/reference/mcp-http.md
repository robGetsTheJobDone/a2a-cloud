# `a2a_pack.mcp.http`

Streamable HTTP transport for MCP (with elicitation support).

Exposes ``POST /mcp`` for client→server requests. When a tool call drives
the skill into ``ctx.collect`` / ``ctx.ask``, the server pushes
``elicitation/create`` requests back to the client interleaved on the SSE
response, and routes the client's responses (delivered via a follow-up
``POST /mcp`` on the same ``Mcp-Session-Id``) to the in-flight future.

Content negotiation:
  * ``Accept: application/json`` (or no Accept) — one-shot JSON response.
    Skills that call ``ctx.collect``/``ctx.ask`` over this path will time
    out, because there is no channel to deliver the request to the client.
  * ``Accept`` includes ``text/event-stream`` — SSE response. Elicitation
    works end-to-end.

Mountable into an existing FastAPI app via :func:`mount_http`, or
serveable standalone via :func:`build_http_app`.

## `ElicitError`  *(class)*



Raised when the client declines/cancels an elicitation request or
returns a JSON-RPC error in response to one.

## `build_http_app`  *(function)*

```python
build_http_app(agent: 'A2AAgent') -> 'FastAPI'
```

Build a minimal FastAPI app exposing only the MCP endpoint.

## `mount_connector_http`  *(function)*

```python
mount_connector_http(app: 'FastAPI', agent: 'A2AAgent', *, prefix: 'str' = '/connector-mcp') -> 'None'
```

Mount connector-optimized MCP with async jobs and structured interrupts.

## `mount_http`  *(function)*

```python
mount_http(app: 'FastAPI', agent: 'A2AAgent', *, prefix: 'str' = '/mcp') -> 'None'
```

Mount the MCP endpoint onto an existing FastAPI app.


*Source: `sdk/a2a-pack/a2a_pack/mcp/http.py`*