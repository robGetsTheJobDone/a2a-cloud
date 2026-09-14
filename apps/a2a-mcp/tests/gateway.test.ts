import { test } from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { AddressInfo } from "node:net";
import os from "node:os";
import path from "node:path";
import { promises as fs } from "node:fs";

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";

import { buildGateway, SEP, type GatewayOptions } from "../src/gateway.js";
import { loadConfig, type EnabledAgent } from "../src/config.js";

/**
 * Fake upstream A2A agent: speaks the JSON-RPC subset the gateway uses
 * (initialize, tools/list, tools/call). One handler per server instance.
 */
function startFakeAgent(opts: {
  tools: Array<{ name: string; description: string; inputSchema: any; outputSchema?: any }>;
  onCall: (name: string, args: any) => any;
  authHeader?: string;
}): Promise<{ url: string; close: () => Promise<void>; requests: any[] }> {
  const requests: any[] = [];
  return new Promise((resolve) => {
    const server = http.createServer((req, res) => {
      if (req.method !== "POST" || req.url !== "/mcp") {
        res.statusCode = 404;
        res.end();
        return;
      }
      const auth = req.headers["authorization"];
      const chunks: Buffer[] = [];
      req.on("data", (c) => chunks.push(c));
      req.on("end", () => {
        const body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
        requests.push({ auth, body });
        let result: any;
        if (body.method === "tools/list") {
          result = { tools: opts.tools };
        } else if (body.method === "tools/call") {
          try {
            result = opts.onCall(body.params.name, body.params.arguments);
          } catch (err) {
            res.setHeader("content-type", "application/json");
            res.end(
              JSON.stringify({
                jsonrpc: "2.0",
                id: body.id,
                error: { code: -32603, message: (err as Error).message },
              }),
            );
            return;
          }
        } else {
          result = {};
        }
        res.setHeader("content-type", "application/json");
        res.end(JSON.stringify({ jsonrpc: "2.0", id: body.id, result }));
      });
    });
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address() as AddressInfo;
      resolve({
        url: `http://127.0.0.1:${port}`,
        requests,
        close: () => new Promise((r) => server.close(() => r())),
      });
    });
  });
}

function startSseAgent(opts: {
  onCall: (body: any, res: http.ServerResponse) => void;
}): Promise<{ url: string; close: () => Promise<void>; requests: any[] }> {
  const requests: any[] = [];
  return new Promise((resolve) => {
    const server = http.createServer((req, res) => {
      if (req.method !== "POST" || req.url !== "/mcp") {
        res.statusCode = 404;
        res.end();
        return;
      }
      const chunks: Buffer[] = [];
      req.on("data", (c) => chunks.push(c));
      req.on("end", () => {
        const body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
        requests.push({ body });
        if (body.method !== "tools/call") {
          res.setHeader("content-type", "application/json");
          res.end(JSON.stringify({ jsonrpc: "2.0", id: body.id, result: {} }));
          return;
        }
        res.writeHead(200, { "content-type": "text/event-stream" });
        opts.onCall(body, res);
      });
    });
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address() as AddressInfo;
      resolve({
        url: `http://127.0.0.1:${port}`,
        requests,
        close: () => new Promise((r) => server.close(() => r())),
      });
    });
  });
}

/** Drive an in-process gateway Server via the SDK's request handlers. */
async function callGateway(
  agents: EnabledAgent[],
  token: string | null,
  method: "tools/list" | "tools/call",
  params: any,
  opts: {
    gateway?: Partial<Omit<GatewayOptions, "agents" | "token" | "server">>;
    extra?: any;
  } = {},
) {
  const mcpServer = new Server(
    { name: "test", version: "0.0.0" },
    { capabilities: { tools: { listChanged: false } } },
  );
  await buildGateway({ agents, token, server: mcpServer, ...opts.gateway });
  // Reach into the server's registered handlers — the SDK exposes them via
  // a private map, so we route through the public schema-keyed lookup we
  // know is wired up.
  const schema = method === "tools/list" ? ListToolsRequestSchema : CallToolRequestSchema;
  const handler = (mcpServer as any)._requestHandlers.get(schema.shape.method.value);
  if (!handler) throw new Error("handler not registered");
  return handler({ method, params }, opts.extra ?? ({} as any));
}

test("tools/list aggregates upstream tools with agent prefix", async () => {
  const upstream = await startFakeAgent({
    tools: [
      {
        name: "greet",
        description: "Greet someone",
        inputSchema: { type: "object", properties: { who: { type: "string" } } },
      },
    ],
    onCall: () => ({ content: [{ type: "text", text: "hi" }], isError: false }),
  });
  try {
    const out = await callGateway(
      [{ name: "greeter", url: upstream.url, addedAt: "now" }],
      null,
      "tools/list",
      {},
    );
    const upstreamTools = out.tools.filter((tool: any) => tool.name.includes(SEP));
    assert.equal(upstreamTools.length, 1);
    assert.equal(upstreamTools[0].name, `greeter${SEP}greet`);
    assert.match(upstreamTools[0].description, /\[greeter\]/);
  } finally {
    await upstream.close();
  }
});

test("tools/list survives a single upstream failure", async () => {
  const good = await startFakeAgent({
    tools: [
      {
        name: "ping",
        description: "p",
        inputSchema: { type: "object", properties: {} },
      },
    ],
    onCall: () => ({ content: [], isError: false }),
  });
  try {
    const out = await callGateway(
      [
        { name: "broken", url: "http://127.0.0.1:1", addedAt: "now" },
        { name: "good", url: good.url, addedAt: "now" },
      ],
      null,
      "tools/list",
      {},
    );
    const names = out.tools
      .map((t: any) => t.name)
      .filter((name: string) => name.includes(SEP));
    assert.deepEqual(names, [`good${SEP}ping`]);
  } finally {
    await good.close();
  }
});

test("tools/call routes by agent prefix and forwards the bearer token", async () => {
  const upstream = await startFakeAgent({
    tools: [],
    onCall: (name, args) => ({
      content: [{ type: "text", text: `called ${name} with ${args.who}` }],
      structuredContent: { result: `hello ${args.who}` },
      isError: false,
    }),
  });
  try {
    const result = await callGateway(
      [{ name: "greeter", url: upstream.url, addedAt: "now" }],
      "tkn-123",
      "tools/call",
      { name: `greeter${SEP}greet`, arguments: { who: "world" } },
    );
    assert.equal(result.isError, false);
    assert.equal(result.structuredContent.result, "hello world");
    const req = upstream.requests.find((r) => r.body.method === "tools/call");
    assert.equal(req.auth, "Bearer tkn-123");
    assert.equal(req.body.params.name, "greet"); // prefix stripped
  } finally {
    await upstream.close();
  }
});

test("tools/call with unknown agent returns soft error", async () => {
  const result = await callGateway([], null, "tools/call", {
    name: `ghost${SEP}skill`,
    arguments: {},
  });
  assert.equal(result.isError, true);
  assert.match(result.content[0].text, /unknown agent: ghost/);
});

test("tools/call without prefix returns soft error", async () => {
  const result = await callGateway([], null, "tools/call", {
    name: "no_prefix",
    arguments: {},
  });
  assert.equal(result.isError, true);
  assert.match(result.content[0].text, /missing agent prefix/);
});

test("tools/call surfaces upstream JSON-RPC error as soft error", async () => {
  const upstream = await startFakeAgent({
    tools: [],
    onCall: () => {
      throw new Error("kaboom");
    },
  });
  try {
    const result = await callGateway(
      [{ name: "boom", url: upstream.url, addedAt: "now" }],
      null,
      "tools/call",
      { name: `boom${SEP}explode`, arguments: {} },
    );
    assert.equal(result.isError, true);
    assert.match(result.content[0].text, /kaboom/);
  } finally {
    await upstream.close();
  }
});

test("tools/call forwards upstream SSE progress notifications", async () => {
  const upstream = await startSseAgent({
    onCall: (body, res) => {
      res.write(
        `data: ${JSON.stringify({
          jsonrpc: "2.0",
          method: "notifications/progress",
          params: { progressToken: "p1", message: "halfway" },
        })}\n\n`,
      );
      res.end(
        `data: ${JSON.stringify({
          jsonrpc: "2.0",
          id: body.id,
          result: {
            content: [{ type: "text", text: "done" }],
            isError: false,
          },
        })}\n\n`,
      );
    },
  });
  const notifications: any[] = [];
  try {
    const result = await callGateway(
      [{ name: "streamer", url: upstream.url, addedAt: "now" }],
      null,
      "tools/call",
      { name: `streamer${SEP}build`, arguments: {} },
      {
        extra: {
          requestId: "req-1",
          sendNotification: async (notification: any) => {
            notifications.push(notification);
          },
        },
      },
    );
    assert.equal(result.isError, false);
    assert.deepEqual(notifications, [
      {
        method: "notifications/progress",
        params: { progressToken: "p1", message: "halfway" },
      },
    ]);
  } finally {
    await upstream.close();
  }
});

test("tools/call reports upstream SSE idle timeout", async () => {
  const upstream = await startSseAgent({
    onCall: (_body, res) => {
      res.write(": connected\n\n");
    },
  });
  try {
    const result = await callGateway(
      [{ name: "slow", url: upstream.url, addedAt: "now" }],
      null,
      "tools/call",
      { name: `slow${SEP}build`, arguments: {} },
      { gateway: { upstreamStreamIdleTimeoutMs: 25 } },
    );
    assert.equal(result.isError, true);
    assert.match(result.content[0].text, /stream idle timeout/);
  } finally {
    await upstream.close();
  }
});

test("tools/call relays upstream elicitation requests", async () => {
  let resolveResponse: (() => void) | undefined;
  const responseSeen = new Promise<void>((resolve) => { resolveResponse = resolve; });
  let upstreamResponse: any = null;
  const requests: any[] = [];
  const server = http.createServer((req, res) => {
    if (req.method !== "POST" || req.url !== "/mcp") {
      res.statusCode = 404;
      res.end();
      return;
    }
    const chunks: Buffer[] = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      const body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
      requests.push({ headers: req.headers, body });
      if (body.method === "tools/call") {
        res.writeHead(200, {
          "content-type": "text/event-stream",
          "Mcp-Session-Id": "sess-1",
        });
        res.write(
          `data: ${JSON.stringify({
            jsonrpc: "2.0",
            id: "ask-1",
            method: "elicitation/create",
            params: { message: "Name?", requestedSchema: { type: "object" } },
          })}\n\n`,
        );
        void responseSeen.then(() => {
          res.end(
            `data: ${JSON.stringify({
              jsonrpc: "2.0",
              id: body.id,
              result: {
                content: [{ type: "text", text: upstreamResponse.result.content.answer }],
                isError: false,
              },
            })}\n\n`,
          );
        });
        return;
      }
      if (body.id === "ask-1" && req.headers["mcp-session-id"] === "sess-1") {
        upstreamResponse = body;
        res.statusCode = 202;
        res.end();
        resolveResponse?.();
        return;
      }
      res.setHeader("content-type", "application/json");
      res.end(JSON.stringify({ jsonrpc: "2.0", id: body.id, result: {} }));
    });
  });
  const upstream = await new Promise<{ url: string; close: () => Promise<void> }>((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address() as AddressInfo;
      resolve({
        url: `http://127.0.0.1:${port}`,
        close: () => new Promise((r) => server.close(() => r())),
      });
    });
  });
  const clientRequests: any[] = [];
  try {
    const result = await callGateway(
      [{ name: "human", url: upstream.url, addedAt: "now" }],
      null,
      "tools/call",
      { name: `human${SEP}run_demo`, arguments: {} },
      {
        extra: {
          requestId: "req-elicitation",
          sendNotification: async () => {},
          sendRequest: async (request: any) => {
            clientRequests.push(request);
            return { action: "accept", content: { answer: "Alice" } };
          },
        },
      },
    );
    assert.equal(result.isError, false);
    assert.equal(result.content[0].text, "Alice");
    assert.equal(clientRequests[0].method, "elicitation/create");
    assert.deepEqual(upstreamResponse.result, {
      action: "accept",
      content: { answer: "Alice" },
    });
    assert.ok(requests.some((r) => r.body.id === "ask-1"));
  } finally {
    await upstream.close();
  }
});

function startFakeControlPlane(): Promise<{ url: string; close: () => Promise<void>; requests: any[] }> {
  const requests: any[] = [];
  return new Promise((resolve) => {
    const server = http.createServer((req, res) => {
      requests.push({ method: req.method, url: req.url, auth: req.headers.authorization });
      if (req.method === "GET" && req.url?.startsWith("/v1/agents/search")) {
        res.setHeader("content-type", "application/json");
        res.end(
          JSON.stringify([
            {
              name: "soc2-evidence",
              description: "Collect SOC2 evidence",
              status: "running",
              public: true,
              url: "https://soc2.example.test",
              score: 0.91,
              match_source: "lexical",
              setup_required: false,
              skills: [{ name: "collect", description: "", tags: ["security"], input_fields: [] }],
            },
          ]),
        );
        return;
      }
      if (req.method === "GET" && req.url === "/v1/agents/soc2-evidence") {
        res.setHeader("content-type", "application/json");
        res.end(
          JSON.stringify({
            name: "soc2-evidence",
            version: "1.0.0",
            status: "running",
            url: "https://soc2.example.test",
            description: "Collect SOC2 evidence",
            card: {
              capabilities: {
                a2a_cloud_import: {
                  mcp: { mode: "generated", path: "/mcp" },
                },
              },
            },
          }),
        );
        return;
      }
      res.statusCode = 404;
      res.end("not found");
    });
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address() as AddressInfo;
      resolve({
        url: `http://127.0.0.1:${port}`,
        requests,
        close: () => new Promise((r) => server.close(() => r())),
      });
    });
  });
}

async function withTempHome<T>(fn: () => Promise<T>): Promise<T> {
  const originalHome = process.env.HOME;
  const tmpHome = await fs.mkdtemp(path.join(os.tmpdir(), "a2a-mcp-gateway-"));
  process.env.HOME = tmpHome;
  try {
    return await fn();
  } finally {
    if (originalHome !== undefined) process.env.HOME = originalHome;
    else delete process.env.HOME;
    await fs.rm(tmpHome, { recursive: true, force: true });
  }
}

test("tools/list includes a2a management tools", async () => {
  const out = await callGateway([], "token", "tools/list", {});
  const names = out.tools.map((tool: any) => tool.name);
  assert.ok(names.includes("a2a_search_agents"));
  assert.ok(names.includes("a2a_add_agent"));
  assert.ok(names.includes("a2a_list_enabled_agents"));
});

test("a2a_search_agents returns visible control-plane results", async () => {
  const cp = await startFakeControlPlane();
  try {
    const result = await callGateway(
      [],
      "access-token",
      "tools/call",
      { name: "a2a_search_agents", arguments: { query: "soc2", tags: ["security"] } },
      { gateway: { apiUrl: cp.url } },
    );
    assert.equal(result.isError, undefined);
    assert.equal(result.structuredContent.agents[0].name, "soc2-evidence");
    assert.equal(result.structuredContent.agents[0].enabled, false);
    assert.equal(cp.requests[0].auth, "Bearer access-token");
    assert.match(cp.requests[0].url, /q=soc2/);
    assert.match(cp.requests[0].url, /tag=security/);
  } finally {
    await cp.close();
  }
});

test("a2a_add_agent persists control-plane agent and reports list change", async () => {
  await withTempHome(async () => {
    const cp = await startFakeControlPlane();
    const notifications: any[] = [];
    try {
      const result = await callGateway(
        [],
        "access-token",
        "tools/call",
        { name: "a2a_add_agent", arguments: { name: "soc2-evidence" } },
        {
          gateway: { apiUrl: cp.url },
          extra: {
            requestId: "req-add",
            sendNotification: async (notification: any) => {
              notifications.push(notification);
            },
            sendRequest: async () => ({}),
          },
        },
      );
      assert.equal(result.isError, undefined);
      assert.match(result.content[0].text, /Added soc2-evidence/);
      assert.equal(notifications[0].method, "notifications/tools/list_changed");

      const cfg = await loadConfig();
      assert.equal(cfg.agents[0].name, "soc2-evidence");
      assert.equal(cfg.agents[0].url, `${cp.url}/v1/agents/soc2-evidence`);
      assert.equal(cfg.agents[0].mcpPath, "/mcp");
    } finally {
      await cp.close();
    }
  });
});
