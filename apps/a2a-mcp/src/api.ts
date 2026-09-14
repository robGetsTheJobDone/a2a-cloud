/** Thin control-plane client used with Keycloak bearer tokens. */
export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(`API ${status}: ${message}`);
    this.name = "ApiError";
  }
}

export interface AgentRow {
  name: string;
  version: string;
  status: string;
  url?: string;
  description?: string;
  card?: Record<string, any>;
}

export interface AgentSearchInputField {
  name: string;
  type?: string | null;
  required?: boolean;
}

export interface AgentSearchSkill {
  name: string;
  description?: string;
  tags?: string[];
  input_fields?: AgentSearchInputField[];
}

export interface AgentSearchRow {
  name: string;
  description: string;
  status: string;
  public: boolean;
  url?: string | null;
  score?: number | null;
  match_source: string;
  llm_provisioning?: string | null;
  setup_required?: boolean;
  skills?: AgentSearchSkill[];
}

export class ControlPlaneClient {
  constructor(
    private apiUrl: string,
    private token: string | null,
  ) {
    this.apiUrl = apiUrl.replace(/\/+$/, "");
  }

  private headers(): Record<string, string> {
    const h: Record<string, string> = {
      accept: "application/json",
      "content-type": "application/json",
    };
    if (this.token) h["authorization"] = `Bearer ${this.token}`;
    return h;
  }

  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const resp = await fetch(`${this.apiUrl}${path}`, {
      method,
      headers: this.headers(),
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    const text = await resp.text();
    if (!resp.ok) {
      throw new ApiError(resp.status, errorDetail(text, resp.statusText));
    }
    if (resp.status === 204) return undefined as T;
    return (text ? JSON.parse(text) : undefined) as T;
  }

  me() {
    return this.request<{ id: number; email: string }>("GET", "/v1/me");
  }

  listAgents() {
    return this.request<AgentRow[]>("GET", "/v1/agents");
  }

  searchAgents(opts: {
    query?: string;
    tags?: string[];
    skill?: string;
    limit?: number;
  }) {
    const params = new URLSearchParams();
    const query = opts.query?.trim();
    if (query) params.set("q", query);
    for (const tag of opts.tags ?? []) {
      const clean = tag.trim();
      if (clean) params.append("tag", clean);
    }
    const skill = opts.skill?.trim();
    if (skill) params.set("skill", skill);
    if (opts.limit !== undefined) params.set("limit", String(opts.limit));
    const qs = params.toString();
    return this.request<AgentSearchRow[]>(
      "GET",
      `/v1/agents/search${qs ? `?${qs}` : ""}`,
    );
  }

  getAgent(name: string) {
    return this.request<AgentRow>("GET", `/v1/agents/${encodeURIComponent(name)}`);
  }
}

function errorDetail(text: string, fallback: string): string {
  if (!text.trim()) return fallback;
  try {
    const parsed = JSON.parse(text);
    const detail = parsed?.detail;
    if (typeof detail === "string") return detail;
    if (detail !== undefined) return JSON.stringify(detail);
    return JSON.stringify(parsed);
  } catch {
    return text;
  }
}
