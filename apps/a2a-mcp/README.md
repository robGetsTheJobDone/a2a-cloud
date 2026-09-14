# a2amcp

MCP gateway for A2A Cloud agents. It runs locally as a stdio MCP server and
exposes deployed A2A agents as tools to Claude Code, Cursor, and other MCP
clients.

This package is the **local stdio gateway**. It is different from the hosted
OAuth MCP connector endpoints used by ChatGPT/Claude web connectors:

```text
Local stdio gateway:        npx -y a2amcp
Remote standard MCP:        https://<agent>.<platform-domain>/mcp
Remote connector MCP:       https://<agent>.<platform-domain>/connector-mcp
Remote orchestrator MCP:    https://api.<platform-domain>/connector-mcp
```

Use `a2amcp` for editor clients that launch a local MCP server process. Use the
remote `/connector-mcp` URL for hosted connector UIs that need OAuth login,
Dynamic Client Registration, async job polling, and structured approval/input
interrupts.

## Pointing at your own platform

By default the gateway talks to the hosted a2a cloud instance. For a
self-hosted platform set one of:

```bash
export A2A_PLATFORM_DOMAIN=example.com   # api.example.com, auth.example.com
export A2A_API_URL=https://api.example.com   # or the control plane URL directly
export A2A_OAUTH_ISSUER=https://auth.example.com/realms/a2acloud
```

`a2amcp login --api <url> --issuer <url>` overrides these per invocation, and a
saved `~/.a2a/credentials.json` from `a2a login` wins over the defaults.

## Quickstart

```bash
npx -y a2amcp login
npx -y a2amcp search "security evidence"
npx -y a2amcp add <agent-name>
npx -y a2amcp doctor
```

Add it to your MCP client config:

```json
{
  "mcpServers": {
    "a2a": {
      "command": "npx",
      "args": ["-y", "a2amcp"]
    }
  }
}
```

Restart the client after adding or removing agents.

## Commands

```bash
a2amcp login              # sign in through Keycloak and save OAuth tokens
a2amcp whoami             # show current account
a2amcp agents             # list agents visible to your account
a2amcp search [query]     # search visible agents by text, tag, or skill
a2amcp add <name>         # expose one agent through this gateway
a2amcp list               # list locally enabled agents
a2amcp remove <name>      # stop exposing an agent
a2amcp doctor             # probe enabled agents and count tools
a2amcp logout             # clear local credentials
```

Running `a2amcp` with no command starts the MCP gateway over stdio.

The gateway also exposes local management tools to MCP clients:

```text
a2a_search_agents          # search visible agents from inside the MCP client
a2a_add_agent              # enable one matching agent dynamically
a2a_list_enabled_agents    # show agents currently exposed by this gateway
```

After `a2a_add_agent`, clients that support tool-list change notifications can
refresh tools immediately. Other clients may need a manual tool refresh or
restart.

`a2amcp login` uses Keycloak Authorization Code + PKCE. It starts a local
loopback callback server, opens the browser, exchanges the code for a Keycloak
access token, and stores the token in `~/.a2a/credentials.json`. For headless
machines, pass an existing Keycloak access token:

```bash
a2amcp login --token "$KEYCLOAK_ACCESS_TOKEN"
```

## Hosted OAuth Connectors

ChatGPT and Claude-style hosted connectors should not run this stdio gateway.
Configure them directly with the remote connector URL:

```text
https://api.<platform-domain>/connector-mcp
https://<agent>.<platform-domain>/connector-mcp
```

Choose OAuth authentication. The server advertises protected-resource metadata,
the client dynamically registers with Keycloak, and the browser consent flow
issues tokens scoped to MCP access. Long-running connector calls return a
`job_id`; the client should poll `chat_result` on the orchestrator connector or
`job_result` on a leaf-agent connector.
