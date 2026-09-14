/**
 * Per-agent JSON-RPC client. Talks to the deployed agent's POST /mcp endpoint.
 *
 * We hand-roll the wire calls (instead of using @modelcontextprotocol/sdk's
 * Client) because (a) the surface we need is tiny — tools/list, tools/call —
 * and (b) we want to send the user's bearer token as a plain Authorization
 * header without coupling to a transport class.
 */
const JSON_RPC = "2.0";
const DEFAULT_RPC_TIMEOUT_MS = 30_000;
const DEFAULT_STREAM_IDLE_TIMEOUT_MS = 120_000;

export interface UpstreamConfig {
  name: string;
  url: string;
  mcpPath?: string;
  token: string | null;
  tokenProvider?: () => Promise<string | null>;
  requestTimeoutMs?: number;
  streamIdleTimeoutMs?: number;
  /** Forwarded to the upstream as ``params._meta.cp_jwt`` on tools/call.
   *  Lets the upstream agent act on the caller's behalf (file/agent CRUD
   *  on /v1/me/*) — same path the platform orchestrator uses. */
  cpJwt?: string | null;
  cpJwtProvider?: () => Promise<string | null>;
  /** Paired with ``cpJwt``. The upstream agent uses this as the base URL
   *  when calling back into /v1/me/*. */
  cpUrl?: string | null;
  /** User's MinIO bucket (``user-<id>-files``). Forwarded as
   *  ``params._meta.bucket`` so agents that touch the workspace
   *  (agent-builder, file tools) get a context whose
   *  ``ctx.workspace.bucket`` resolves to the right bucket. */
  bucket?: string | null;
}

export interface UpstreamTool {
  name: string;
  description?: string;
  inputSchema: unknown;
  outputSchema?: unknown;
}

export interface UpstreamCallResult {
  content: Array<{ type: string; text?: string; [k: string]: unknown }>;
  structuredContent?: unknown;
  isError?: boolean;
}

export interface UpstreamServerRequest {
  id: string | number;
  method: string;
  params?: Record<string, unknown>;
}

export class UpstreamError extends Error {
  constructor(
    public agent: string,
    public status: number,
    message: string,
  ) {
    super(`upstream ${agent}: ${message}`);
    this.name = "UpstreamError";
  }
}

let _nextId = 1;

function normalizeMcpPath(path: string | undefined): string {
  if (!path) return "/mcp";
  const trimmed = path.trim();
  if (!trimmed) return "/mcp";
  return trimmed.startsWith("/") ? trimmed : `/${trimmed}`;
}

export class UpstreamAgent {
  constructor(private cfg: UpstreamConfig) {
    this.cfg.url = cfg.url.replace(/\/+$/, "");
    this.cfg.mcpPath = normalizeMcpPath(cfg.mcpPath);
  }

  get name(): string {
    return this.cfg.name;
  }

  private mcpUrl(): string {
    return `${this.cfg.url}${this.cfg.mcpPath ?? "/mcp"}`;
  }

  private async bearerToken(): Promise<string | null> {
    if (this.cfg.tokenProvider) return this.cfg.tokenProvider();
    return this.cfg.token ?? null;
  }

  private async cpJwt(): Promise<string | null> {
    if (this.cfg.cpJwtProvider) return this.cfg.cpJwtProvider();
    return this.cfg.cpJwt ?? null;
  }

  private async rpc<T>(method: string, params: unknown): Promise<T> {
    const id = _nextId++;
    const timeoutMs = this.cfg.requestTimeoutMs ?? DEFAULT_RPC_TIMEOUT_MS;
    const controller = new AbortController();
    let timedOut = false;
    const timeout = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeoutMs);
    const headers: Record<string, string> = {
      "content-type": "application/json",
      accept: "application/json",
    };
    const token = await this.bearerToken();
    if (token) headers["authorization"] = `Bearer ${token}`;

    try {
      const resp = await fetch(this.mcpUrl(), {
        method: "POST",
        headers,
        body: JSON.stringify({ jsonrpc: JSON_RPC, id, method, params }),
        signal: controller.signal,
      });

      if (!resp.ok) {
        const text = await resp.text().catch(() => "");
        throw new UpstreamError(this.cfg.name, resp.status, text || resp.statusText);
      }

      const env: any = await resp.json();
      if (env?.error) {
        throw new UpstreamError(
          this.cfg.name,
          500,
          `${env.error.code}: ${env.error.message}`,
        );
      }
      return env?.result as T;
    } catch (err) {
      if (timedOut) {
        throw new UpstreamError(
          this.cfg.name,
          504,
          `request timed out after ${timeoutMs}ms`,
        );
      }
      throw err;
    } finally {
      clearTimeout(timeout);
    }
  }

  async listTools(): Promise<UpstreamTool[]> {
    const out = await this.rpc<{ tools: UpstreamTool[] }>("tools/list", {});
    return out?.tools ?? [];
  }

  async callTool(
    name: string,
    arguments_: Record<string, unknown>,
    opts: {
      onNotification?: (notif: {
        method: string;
        params?: Record<string, unknown>;
      }) => void | Promise<void>;
      onRequest?: (req: UpstreamServerRequest) => unknown | Promise<unknown>;
      progressToken?: string | number;
    } = {},
  ): Promise<UpstreamCallResult> {
    const meta: Record<string, unknown> = {};
    const cpJwt = await this.cpJwt();
    if (cpJwt) meta.cp_jwt = cpJwt;
    if (this.cfg.cpUrl) meta.cp_url = this.cfg.cpUrl;
    if (this.cfg.bucket) meta.bucket = this.cfg.bucket;
    if (opts.progressToken !== undefined) meta.progressToken = opts.progressToken;
    const params: Record<string, unknown> = { name, arguments: arguments_ };
    if (Object.keys(meta).length > 0) params._meta = meta;
    return this.streamingCallTool(params, {
      onNotification: opts.onNotification,
      onRequest: opts.onRequest,
    });
  }

  /**
   * tools/call always negotiates SSE on the wire — upstream agents can
   * take minutes (agent-builder.build runs an LLM loop + sandbox tests +
   * a deploy + a wait-for-live-card poll), and a plain JSON POST gets
   * killed by ingress idle timeouts (~60s default on traefik). The
   * upstream's SSE response interleaves any elicitation/create requests
   * and a final tools/call result; here we relay server-to-client
   * requests through the MCP client and return the final result.
   */
  private async streamingCallTool(
    params: Record<string, unknown>,
    handlers: {
      onNotification?: (notif: {
        method: string;
        params?: Record<string, unknown>;
      }) => void | Promise<void>;
      onRequest?: (req: UpstreamServerRequest) => unknown | Promise<unknown>;
    } = {},
  ): Promise<UpstreamCallResult> {
    const id = _nextId++;
    const idleTimeoutMs =
      this.cfg.streamIdleTimeoutMs ?? DEFAULT_STREAM_IDLE_TIMEOUT_MS;
    const controller = new AbortController();
    let timedOut = false;
    let idleTimer: ReturnType<typeof setTimeout> | undefined;
    const resetIdleTimer = () => {
      if (idleTimer) clearTimeout(idleTimer);
      idleTimer = setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, idleTimeoutMs);
    };
    resetIdleTimer();
    const headers: Record<string, string> = {
      "content-type": "application/json",
      accept: "text/event-stream, application/json",
    };
    const token = await this.bearerToken();
    if (token) headers["authorization"] = `Bearer ${token}`;

    let resp: Response;
    try {
      resp = await fetch(this.mcpUrl(), {
        method: "POST",
        headers,
        body: JSON.stringify({
          jsonrpc: JSON_RPC, id, method: "tools/call", params,
        }),
        signal: controller.signal,
      });
    } catch (err) {
      clearTimeout(idleTimer);
      if (timedOut) {
        throw new UpstreamError(
          this.cfg.name,
          504,
          `stream idle timeout after ${idleTimeoutMs}ms`,
        );
      }
      throw err;
    }
    resetIdleTimer();

    if (!resp.ok) {
      const text = await resp.text().catch(() => "");
      clearTimeout(idleTimer);
      throw new UpstreamError(this.cfg.name, resp.status, text || resp.statusText);
    }

    const ctype = resp.headers.get("content-type") || "";
    if (!ctype.includes("text/event-stream")) {
      // Server fell back to JSON (older a2a-pack or non-SSE upstream).
      const env: any = await resp.json();
      if (env?.error) {
        clearTimeout(idleTimer);
        throw new UpstreamError(
          this.cfg.name, 500,
          `${env.error.code}: ${env.error.message}`,
        );
      }
      clearTimeout(idleTimer);
      return env?.result as UpstreamCallResult;
    }

    const upstreamSessionId = resp.headers.get("mcp-session-id");

    if (!resp.body) {
      clearTimeout(idleTimer);
      throw new UpstreamError(this.cfg.name, 502, "empty stream body");
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    try {
      while (true) {
        let done: boolean;
        let value: Uint8Array | undefined;
        try {
          ({ done, value } = await reader.read());
        } catch (err) {
          if (timedOut) {
            throw new UpstreamError(
              this.cfg.name,
              504,
              `stream idle timeout after ${idleTimeoutMs}ms`,
            );
          }
          throw err;
        }
        if (value) {
          resetIdleTimer();
          buf += decoder.decode(value, { stream: true });
        }
        let sep = buf.indexOf("\n\n");
        while (sep !== -1) {
          const frame = buf.slice(0, sep);
          buf = buf.slice(sep + 2);
          const dataLines: string[] = [];
          for (const ln of frame.split("\n")) {
            if (ln.startsWith("data:")) dataLines.push(ln.slice(5).trimStart());
          }
          if (dataLines.length > 0) {
            const raw = dataLines.join("\n");
            if (raw === "[DONE]") {
              sep = buf.indexOf("\n\n");
              continue;
            }
            let msg: any;
            try { msg = JSON.parse(raw); } catch { msg = null; }
            if (msg && msg.id === id) {
              if (msg.error) {
                throw new UpstreamError(
                  this.cfg.name, 500,
                  `${msg.error.code}: ${msg.error.message}`,
                );
              }
              return msg.result as UpstreamCallResult;
            }
            // Otherwise it's a server→client message. Notifications
            // (no id) are best-effort. Requests (has id, has method), such
            // as elicitation/create, must be relayed and answered upstream.
            if (msg && typeof msg.method === "string" && !("id" in msg) && handlers.onNotification) {
              try {
                await handlers.onNotification({
                  method: msg.method,
                  params: msg.params as Record<string, unknown> | undefined,
                });
              } catch {
                // Best-effort; never break the stream on a notify error.
              }
            } else if (msg && typeof msg.method === "string" && "id" in msg) {
              await this.relayServerRequest(upstreamSessionId, {
                id: msg.id as string | number,
                method: msg.method,
                params: msg.params as Record<string, unknown> | undefined,
              }, handlers.onRequest);
              resetIdleTimer();
            }
          }
          sep = buf.indexOf("\n\n");
        }
        if (done) break;
      }
    } finally {
      clearTimeout(idleTimer);
      try { reader.releaseLock(); } catch { /* ignore */ }
    }
    throw new UpstreamError(this.cfg.name, 502, "stream ended without result");
  }

  private async relayServerRequest(
    upstreamSessionId: string | null,
    req: UpstreamServerRequest,
    onRequest?: (req: UpstreamServerRequest) => unknown | Promise<unknown>,
  ): Promise<void> {
    if (!upstreamSessionId) {
      throw new UpstreamError(
        this.cfg.name,
        502,
        `upstream request ${req.method} missing Mcp-Session-Id`,
      );
    }
    let envelope: Record<string, unknown>;
    try {
      if (!onRequest) throw new Error(`no client request relay for ${req.method}`);
      const result = await onRequest(req);
      envelope = { jsonrpc: JSON_RPC, id: req.id, result };
    } catch (err) {
      envelope = {
        jsonrpc: JSON_RPC,
        id: req.id,
        error: {
          code: -32603,
          message: err instanceof Error ? err.message : String(err),
        },
      };
    }

    const headers: Record<string, string> = {
      "content-type": "application/json",
      accept: "application/json",
      "mcp-session-id": upstreamSessionId,
    };
    const token = await this.bearerToken();
    if (token) headers.authorization = `Bearer ${token}`;
    const resp = await fetch(this.mcpUrl(), {
      method: "POST",
      headers,
      body: JSON.stringify(envelope),
    });
    if (!resp.ok) {
      const text = await resp.text().catch(() => "");
      throw new UpstreamError(
        this.cfg.name,
        resp.status,
        text || `failed to deliver response for ${req.method}`,
      );
    }
  }
}
