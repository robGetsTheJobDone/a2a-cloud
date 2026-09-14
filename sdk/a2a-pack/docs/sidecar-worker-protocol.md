# Sidecar Worker Protocol

The common sidecar owns public A2A/MCP/HTTP endpoints. Language SDKs own native
handler execution. A target-language SDK must:

1. compile native declarations to `.a2a/agent.dsl.json`
2. run a local worker HTTP server
3. implement one endpoint per handler:

```text
POST /_a2a/invoke/{handler}
```

## Request

The sidecar sends `SidecarWorkerRequest` JSON:

```json
{
  "agent": "research-agent",
  "skill": "ask",
  "handler": "askHandler",
  "arguments": { "prompt": "..." },
  "grant": "optional workspace grant token",
  "llm_creds": {
    "base_url": "https://...",
    "api_key": "...",
    "model": "...",
    "temperature_mode": "omit"
  },
  "composition": {},
  "consumer_config": {},
  "consumer_secrets": {},
  "cp_jwt": "optional forwarded control-plane JWT",
  "cp_url": "optional control-plane base URL",
  "auth": {}
}
```

Required fields: `agent`, `skill`, `handler`. `arguments` defaults to `{}`.

The sidecar validates `arguments` against the compiled skill request shape before
calling the worker. Workers may do deeper runtime validation with native types.

## Response

Workers must return `SidecarWorkerResponse` JSON:

```json
{
  "result": "handler return value",
  "events": [],
  "artifacts": []
}
```

`result` is required. Worker failures should use HTTP 4xx/5xx; the sidecar maps
those failures to public protocol errors.

## Direct Invoke

Public callers use:

```text
POST /invoke/{skill}
```

with:

```json
{
  "arguments": {},
  "grant": null,
  "llm_creds": null,
  "composition": null,
  "consumer_config": null,
  "consumer_secrets": null,
  "cp_jwt": null,
  "cp_url": null,
  "auth": null
}
```

The sidecar maps `skill` to the DSL `handler` and forwards a
`SidecarWorkerRequest` to the worker endpoint.
