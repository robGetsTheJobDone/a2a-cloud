# Agent Card spec should require typed `input_schema` per skill

## Problem

A2A Agent Cards advertise skills with `name`, `description`, and `inputModes`,
but no machine-readable parameter schema. Callers that discover an agent via
the registry cannot tell which arguments a skill requires until they invoke
it and parse the validation error.

In practice this forces trial-and-error:

```json
// guess 1
{"tweet": "..."}        // 422: missing required parameter: tweets

// guess 2
{"text": "..."}         // 422: missing required parameter: tweets

// guess 3 — works
{"tweets": ["..."]}
```

Each guess costs a network round-trip and LLM token budget. Worst case the
caller gives up after N tries and the user sees a phantom failure.

## Compare: MCP `tools/list`

MCP solved this in 2024. Every tool returned by `tools/list` carries a full
JSONSchema:

```json
{
  "name": "post_tweet",
  "description": "...",
  "inputSchema": {
    "type": "object",
    "required": ["tweets"],
    "properties": {
      "tweets": {"type": "array", "items": {"type": "string"}}
    }
  }
}
```

Clients validate args locally before sending. LLM gets the schema in the
tool definition. Zero trial-and-error.

## Proposal

Add a required `input_schema` field to the `Skill` object in the Agent Card
spec, typed as a JSONSchema draft 2020-12 object.

```yaml
skills:
  - name: post_tweet
    description: Post a thread to X.
    input_schema:
      type: object
      required: [tweets]
      properties:
        tweets:
          type: array
          items: {type: string}
    tags: [social, x]
```

Registry list endpoints (`GET /v1/agents`, equivalent of `tools/list`) MUST
include the schema inline — not behind a second round-trip to
`/.well-known/agent-card`. Discovery should be one call.

## Migration

- Mark `input_schema` optional in the spec for one release; warn on cards
  missing it.
- Promote to required in the following major.
- Reference implementations (this repo, others) emit it from the existing
  `SkillCard.input_schema` field — no agent-author changes needed.

## Why this matters

A2A's pitch is "agents find each other dynamically." Dynamic discovery
without typed schemas pushes the typing burden onto the LLM at runtime,
which is the slowest and most expensive validator in the stack. The MCP
shape is proven; copying it costs nothing and removes a whole class of
failure mode.
