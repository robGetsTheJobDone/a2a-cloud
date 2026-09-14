/**
 * MCP gateway server. Fans out tools/list and tools/call to each enabled
 * upstream agent (deployed on the platform). Tool names are prefixed with the
 * agent slug so multiple agents can coexist in one MCP namespace.
 *
 * Naming: ``{agent}__{skill}``. Double underscore separator. Skill names
 * containing ``__`` are not supported.
 */
import { createRequire } from "node:module";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ElicitResultSchema,
  ListToolsRequestSchema,
  ResultSchema,
} from "@modelcontextprotocol/sdk/types.js";

import { loadConfig, type EnabledAgent } from "./config.js";
import { ApiError, ControlPlaneClient } from "./api.js";
import { loadCredentials, resolveApiUrl, type Credentials } from "./credentials.js";
import { refreshCredentialsIfNeeded } from "./oauth.js";
import {
  enableAgentByName,
  searchVisibleAgents,
  summarizeAgent,
} from "./registry.js";
import { UpstreamAgent, UpstreamError, type UpstreamTool } from "./upstream.js";

const require = createRequire(import.meta.url);
const pkg = require("../package.json") as { version: string };

export const SEP = "__";
const SEARCH_AGENTS_TOOL = "a2a_search_agents";
const ADD_AGENT_TOOL = "a2a_add_agent";
const LIST_ENABLED_AGENTS_TOOL = "a2a_list_enabled_agents";

const MANAGEMENT_TOOLS: Array<UpstreamTool & { name: string }> = [
  {
    name: SEARCH_AGENTS_TOOL,
    description:
      "[a2a] Search agents visible to your account by text, tag, or skill before enabling them in this MCP gateway.",
    inputSchema: {
      type: "object",
      properties: {
        query: { type: "string", description: "Free-text search query." },
        tags: {
          type: "array",
          items: { type: "string" },
          description: "Optional tags that matching agents must have.",
        },
        skill: { type: "string", description: "Optional skill name filter." },
        limit: {
          type: "integer",
          minimum: 1,
          maximum: 25,
          default: 8,
          description: "Maximum number of results.",
        },
      },
      additionalProperties: false,
    },
  },
  {
    name: ADD_AGENT_TOOL,
    description:
      "[a2a] Enable an agent in this MCP gateway. Newly added agent tools appear on the next tools/list refresh.",
    inputSchema: {
      type: "object",
      properties: {
        name: { type: "string", description: "Agent name to enable." },
        url: {
          type: "string",
          description: "Optional direct agent base URL; skips control-plane lookup.",
        },
        mcp_path: {
          type: "string",
          description: "Optional MCP path when url is provided. Defaults to /mcp.",
        },
      },
      required: ["name"],
      additionalProperties: false,
    },
  },
  {
    name: LIST_ENABLED_AGENTS_TOOL,
    description: "[a2a] List agents currently enabled in this local MCP gateway.",
    inputSchema: {
      type: "object",
      properties: {},
      additionalProperties: false,
    },
  },
];

export interface GatewayOptions {
  /** Override the enabled agents (otherwise read from ~/.a2a/mcp.json). */
  agents?: EnabledAgent[];
  /** Override the bearer token (otherwise read from ~/.a2a/credentials.json). */
  token?: string | null;
  /** Test/advanced override for one-shot upstream JSON-RPC calls. */
  upstreamRequestTimeoutMs?: number;
  /** Test/advanced override for idle SSE upstream tools/call streams. */
  upstreamStreamIdleTimeoutMs?: number;
  /** Optional control-plane API override for management tools (tests/advanced). */
  apiUrl?: string;
  /** Optional override for the underlying MCP Server (tests). */
  server?: Server;
}

export interface GatewayHandle {
  server: Server;
  upstreams: Map<string, UpstreamAgent>;
}

export async function buildGateway(opts: GatewayOptions = {}): Promise<GatewayHandle> {
  const cfg = opts.agents ?? (await loadConfig()).agents;
  let creds: Credentials | null = opts.token !== undefined ? null : await loadCredentials();
  async function refreshedToken(): Promise<string | null> {
    if (opts.token !== undefined) return opts.token;
    if (!creds) return null;
    creds = await refreshCredentialsIfNeeded(creds);
    return creds.token;
  }
  const token = opts.token !== undefined ? opts.token : creds?.token ?? null;
  // Forward CP credentials + bucket as params._meta so upstream agents
  // can act on behalf of the caller (mirrors the platform-orchestrator
  // call shape). When the user isn't logged in, _meta is omitted and
  // upstream sees the same unauthenticated context as before.
  const apiUrl = resolveApiUrl(opts.apiUrl ?? creds?.apiUrl);
  const cpUrl = opts.apiUrl ?? creds?.apiUrl ?? null;
  const bucket = creds?.bucket ?? null;

  const upstreams = new Map<string, UpstreamAgent>();

  function makeUpstream(a: EnabledAgent): UpstreamAgent {
    return new UpstreamAgent({
      name: a.name, url: a.url, mcpPath: a.mcpPath, token,
      tokenProvider: refreshedToken,
      cpJwtProvider: refreshedToken,
      cpUrl, bucket,
      requestTimeoutMs: opts.upstreamRequestTimeoutMs,
      streamIdleTimeoutMs: opts.upstreamStreamIdleTimeoutMs,
    });
  }

  function upsertUpstream(a: EnabledAgent): void {
    upstreams.set(a.name, makeUpstream(a));
  }

  async function syncUpstreamsFromConfig(): Promise<void> {
    if (opts.agents) return;
    const latest = (await loadConfig()).agents;
    const names = new Set(latest.map((agent) => agent.name));
    for (const name of upstreams.keys()) {
      if (!names.has(name)) upstreams.delete(name);
    }
    for (const agent of latest) {
      upsertUpstream(agent);
    }
  }

  for (const a of cfg) {
    upsertUpstream(a);
  }

  const server =
    opts.server ??
    new Server(
      { name: "a2amcp", version: pkg.version },
      { capabilities: { tools: { listChanged: true } } },
    );

  server.setRequestHandler(ListToolsRequestSchema, async () => {
    await syncUpstreamsFromConfig();
    const tools: Array<UpstreamTool & { name: string }> = [...MANAGEMENT_TOOLS];
    await Promise.all(
      [...upstreams.values()].map(async (u) => {
        try {
          const upstreamTools = await u.listTools();
          for (const t of upstreamTools) {
            tools.push({
              ...t,
              name: `${u.name}${SEP}${t.name}`,
              description: t.description
                ? `[${u.name}] ${t.description}`
                : `[${u.name}] ${t.name}`,
            });
          }
        } catch (err) {
          // Silently skip an upstream that's down — surfacing an error here
          // would break the client's whole tool list. The user can debug via
          // `a2amcp doctor`.
          process.stderr.write(
            `a2amcp: skipping ${u.name}: ${(err as Error).message}\n`,
          );
        }
      }),
    );
    return { tools };
  });

  server.setRequestHandler(CallToolRequestSchema, async (req, extra) => {
    const fullName = req.params.name;
    const args = (req.params.arguments as Record<string, unknown>) ?? {};
    if (fullName === SEARCH_AGENTS_TOOL) {
      try {
        const rows = await searchVisibleAgents(
          new ControlPlaneClient(apiUrl, await refreshedToken()),
          {
            query: stringArg(args.query),
            tags: stringArrayArg(args.tags),
            skill: stringArg(args.skill),
            limit: numberArg(args.limit, 8),
          },
        );
        const enabled = new Set((await loadConfig()).agents.map((agent) => agent.name));
        return {
          content: [
            {
              type: "text" as const,
              text: rows.length
                ? rows
                    .map((row) => {
                      const marker = enabled.has(row.name) ? "enabled" : "available";
                      return `${row.name} (${marker}) - ${summarizeAgent(row)}`;
                    })
                    .join("\n")
                : "No matching agents.",
            },
          ],
          structuredContent: {
            agents: rows.map((row) => ({
              ...row,
              enabled: enabled.has(row.name),
            })),
          },
        } as any;
      } catch (err) {
        return _softError(errorMessage(err)) as any;
      }
    }

    if (fullName === ADD_AGENT_TOOL) {
      const name = stringArg(args.name);
      if (!name) return _softError("name is required") as any;
      try {
        const added = await enableAgentByName({
          cp: stringArg(args.url)
            ? undefined
            : new ControlPlaneClient(apiUrl, await refreshedToken()),
          apiUrl,
          name,
          url: stringArg(args.url),
          mcpPath: stringArg(args.mcp_path),
        });
        upsertUpstream(added);
        try {
          await extra.sendNotification({
            method: "notifications/tools/list_changed",
            params: {},
          } as any);
        } catch {
          // best effort; clients can still refresh tools explicitly
        }
        return {
          content: [
            {
              type: "text" as const,
              text: `${added.replaced ? "Updated" : "Added"} ${added.name} -> ${added.endpoint}. Refresh tools to see its MCP tools.`,
            },
          ],
          structuredContent: { agent: added },
        } as any;
      } catch (err) {
        return _softError(errorMessage(err)) as any;
      }
    }

    if (fullName === LIST_ENABLED_AGENTS_TOOL) {
      const cfg = await loadConfig();
      return {
        content: [
          {
            type: "text" as const,
            text: cfg.agents.length
              ? cfg.agents
                  .map((agent) => `${agent.name} ${agent.url.replace(/\/+$/, "")}${agent.mcpPath ?? "/mcp"}`)
                  .join("\n")
              : "No agents enabled.",
          },
        ],
        structuredContent: { agents: cfg.agents },
      } as any;
    }

    const idx = fullName.indexOf(SEP);
    if (idx < 0) {
      return _softError(`tool name missing agent prefix: ${fullName}`) as any;
    }
    const agentName = fullName.slice(0, idx);
    const toolName = fullName.slice(idx + SEP.length);
    const upstream = upstreams.get(agentName);
    if (!upstream) {
      return _softError(`unknown agent: ${agentName}`) as any;
    }
    // Honor the client's progressToken if it supplied one; else fall
    // back to the request id. Either way, when upstream emits
    // notifications/progress we forward them via the SDK's
    // sendNotification so clients like Claude Code reset their idle
    // tool-call timer and don't abort long builds.
    const clientToken = (req.params._meta as any)?.progressToken as
      | string
      | number
      | undefined;
    const progressToken = clientToken ?? (extra.requestId as string | number);
    try {
      const result = await upstream.callTool(
        toolName,
        args,
        {
          progressToken,
          onNotification: async (notif) => {
            try {
              await extra.sendNotification(notif as any);
            } catch {
              // best effort
            }
          },
          onRequest: async (upstreamReq) => {
            const schema = upstreamReq.method === "elicitation/create"
              ? ElicitResultSchema
              : ResultSchema;
            return extra.sendRequest(
              {
                method: upstreamReq.method,
                params: upstreamReq.params,
              } as any,
              schema as any,
            );
          },
        },
      );
      // Pass through the upstream's CallToolResult as-is. Upstreams already
      // populate content / structuredContent / isError per the MCP spec.
      // Cast through `unknown` because the SDK's ServerResult union has
      // additional task-shaped variants we don't produce.
      return result as unknown as any;
    } catch (err) {
      const msg =
        err instanceof UpstreamError
          ? err.message
          : `${(err as Error).name}: ${(err as Error).message}`;
      return _softError(msg) as any;
    }
  });

  return { server, upstreams };
}

export async function runStdio(opts: GatewayOptions = {}): Promise<void> {
  const { server } = await buildGateway(opts);
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

function _softError(message: string) {
  return {
    content: [{ type: "text" as const, text: message }],
    isError: true,
  };
}

function stringArg(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function stringArrayArg(value: unknown): string[] | undefined {
  if (!Array.isArray(value)) return undefined;
  return value
    .filter((item): item is string => typeof item === "string")
    .map((item) => item.trim())
    .filter(Boolean);
}

function numberArg(value: unknown, fallback: number): number {
  const number = typeof value === "number"
    ? value
    : typeof value === "string"
      ? Number.parseInt(value, 10)
      : fallback;
  if (!Number.isFinite(number)) return fallback;
  return Math.min(25, Math.max(1, Math.floor(number)));
}

function errorMessage(err: unknown): string {
  return err instanceof ApiError ? err.message : (err as Error).message;
}
