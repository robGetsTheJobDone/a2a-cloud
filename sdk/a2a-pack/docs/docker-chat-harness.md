# Docker Chat Harness

`a2a chat` runs one local agent in Docker Compose with only the resources that
agent declares in `a2a.yaml`.

It is meant for the tight edit/test loop before using the full MicroCash
devcontainer stack.

## Start

```bash
a2a chat --project path/to/agent --env-file .env.local
```

Open the chat/run console:

```text
http://127.0.0.1:8000/_dev
```

The harness writes its generated files to:

```text
path/to/agent/.a2a/chat/
```

Use `--detach` to leave the compose services running in the background:

```bash
a2a chat --project path/to/agent --detach
```

Stop it:

```bash
a2a chat --project path/to/agent --down
```

Remove local Qdrant/Postgres data volumes too:

```bash
a2a chat --project path/to/agent --down --volumes
```

## Resource Mapping

Vector memory declarations start Qdrant and set:

```text
A2A_MEMORY_VECTOR_URL=http://qdrant:6333
A2A_QDRANT_URL=http://qdrant:6333
QDRANT_URL=http://qdrant:6333
```

Agents using `ctx.memory.for_tier("vector")` will use local Qdrant when those
env vars are present.

Database declarations start local Postgres as the Neon-compatible development
stand-in. The declared database env name is populated, for example:

```yaml
resources:
  databases:
    - name: app
      provider: neon
      engine: postgres
      env:
        url: DATABASE_URL
```

becomes:

```text
DATABASE_URL=postgresql://a2a:a2a@postgres:5432/app
```

## LLM Keys

The command loads the selected env file. If it contains `OPENAI_API_KEY`,
`a2a chat` maps it to `AGENT_LLM_KEY` for local `ctx.llm` usage unless
`AGENT_LLM_KEY` is already set.

Secret values are passed through the compose environment and are not written
into the generated compose file.
