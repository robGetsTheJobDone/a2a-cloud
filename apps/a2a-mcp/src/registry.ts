import { ControlPlaneClient, type AgentRow, type AgentSearchRow } from "./api.js";
import { addAgent, loadConfig, type EnabledAgent } from "./config.js";

export type EnabledAgentResult = EnabledAgent & {
  endpoint: string;
  replaced: boolean;
};

export function normalizeMcpPath(path: unknown): string {
  if (typeof path !== "string" || !path.trim()) return "/mcp";
  const trimmed = path.trim();
  return trimmed.startsWith("/") ? trimmed : `/${trimmed}`;
}

export function mcpTargetForAgent(
  row: { name: string; url?: string | null; card?: Record<string, any> },
  apiUrl: string,
): { url?: string; mcpPath: string } {
  const imported = row.card?.capabilities?.a2a_cloud_import;
  const mcp = imported?.mcp;
  if (mcp?.mode === "generated") {
    return {
      url: `${apiUrl.replace(/\/+$/, "")}/v1/agents/${encodeURIComponent(row.name)}`,
      mcpPath: normalizeMcpPath(mcp.path),
    };
  }
  return {
    url: mcp?.base_url || row.url || undefined,
    mcpPath: normalizeMcpPath(mcp?.path || row.card?.mcp_endpoint),
  };
}

export async function searchVisibleAgents(
  cp: ControlPlaneClient,
  opts: {
    query?: string;
    tags?: string[];
    skill?: string;
    limit?: number;
  },
): Promise<AgentSearchRow[]> {
  return cp.searchAgents(opts);
}

export async function enableAgentByName(opts: {
  cp?: ControlPlaneClient;
  apiUrl: string;
  name: string;
  url?: string;
  mcpPath?: string;
}): Promise<EnabledAgentResult> {
  const before = await loadConfig();
  const existing = before.agents.find((agent) => agent.name === opts.name);
  let url = opts.url?.trim();
  let mcpPath = normalizeMcpPath(opts.mcpPath);
  let description: string | undefined;

  if (!url) {
    if (!opts.cp) throw new Error("control-plane client required when url is not provided");
    const row = await opts.cp.getAgent(opts.name);
    const target = mcpTargetForAgent(row, opts.apiUrl);
    url = target.url;
    mcpPath = target.mcpPath;
    description = row.description;
  }

  if (!url) throw new Error(`agent ${opts.name} has no public URL yet`);

  const agent: EnabledAgent = {
    name: opts.name,
    url,
    mcpPath,
    description,
    addedAt: new Date().toISOString(),
  };
  await addAgent(agent);
  return {
    ...agent,
    endpoint: `${url.replace(/\/+$/, "")}${mcpPath}`,
    replaced: existing !== undefined,
  };
}

export function summarizeAgent(row: AgentRow | AgentSearchRow): string {
  const skills = "skills" in row && Array.isArray(row.skills)
    ? row.skills.map((skill) => skill.name).filter(Boolean).slice(0, 4)
    : [];
  const skillText = skills.length ? ` skills=${skills.join(",")}` : "";
  const url = row.url || "-";
  return `${row.name} [${row.status}] ${url}${skillText}`;
}
