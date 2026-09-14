# `a2a_pack.mcp.server`

Core MCP JSON-RPC handler — transport-agnostic.

The handler maps the MCP wire protocol onto an :class:`A2AAgent`:

- ``initialize`` advertises tool support and echoes the server identity
  from the agent's :class:`AgentCard`.
- ``tools/list`` enumerates the agent's skills.
- ``tools/call`` dispatches into :meth:`A2AAgent.invoke_json`.

A transport (stdio, HTTP) calls :meth:`MCPServer.handle` with each
decoded JSON-RPC message and forwards the returned dict back to the
client. Notifications return ``None``.

## `MCPServer`  *(class)*

```python
MCPServer(agent: 'A2AAgent', *, context_builder: 'ContextBuilder | None' = None, extra_tools: 'list[dict[str, Any]] | None' = None, extra_tool_handler: 'ExtraToolHandler | None' = None, tool_call_handler: 'ToolCallHandler | None' = None, tools_provider: 'ToolsProvider | None' = None) -> 'None'
```

Transport-agnostic MCP request handler for an :class:`A2AAgent`.

### `__init__`  *(method)*

```python
__init__(self, agent: 'A2AAgent', *, context_builder: 'ContextBuilder | None' = None, extra_tools: 'list[dict[str, Any]] | None' = None, extra_tool_handler: 'ExtraToolHandler | None' = None, tool_call_handler: 'ToolCallHandler | None' = None, tools_provider: 'ToolsProvider | None' = None) -> 'None'
```

*(no docstring)*

### `handle`  *(method)*

```python
handle(self, message: 'dict[str, Any]') -> 'dict[str, Any] | None'
```

Dispatch one JSON-RPC message. Returns response or ``None`` for notifications.

## `parse_message`  *(function)*

```python
parse_message(raw: 'str | bytes') -> 'dict[str, Any]'
```

Parse a JSON-RPC frame; raises :class:`_MCPError` with PARSE_ERROR.

## `skills_to_tools`  *(function)*

```python
skills_to_tools(agent: 'A2AAgent', *, extra_tools: 'list[dict[str, Any]] | None' = None) -> 'list[dict[str, Any]]'
```

Render the agent's skills as MCP tool descriptors.

## `tool_call_error`  *(function)*

```python
tool_call_error(message: 'str') -> 'dict[str, Any]'
```

Wrap a tool-side failure as an MCP CallToolResult with isError=true.

## `tool_call_result`  *(function)*

```python
tool_call_result(spec: 'SkillSpec', value: 'Any') -> 'dict[str, Any]'
```

Wrap a skill return value as an MCP CallToolResult.


*Source: `sdk/a2a-pack/a2a_pack/mcp/server.py`*