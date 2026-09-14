/**
 * Local config at ~/.a2a/mcp.json — which agents the gateway should expose.
 *
 * We store the agent URL alongside the name so the gateway never needs to hit
 * the control plane during startup. Adding/removing an agent is a single
 * file write; the MCP client picks up the change on the next list_tools.
 */
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";

export interface EnabledAgent {
  name: string;
  url: string;
  mcpPath?: string;
  description?: string;
  addedAt: string;
}

export interface GatewayConfig {
  version: 1;
  agents: EnabledAgent[];
}

const cfgDir = () => path.join(os.homedir(), ".a2a");
const cfgFile = () => path.join(cfgDir(), "mcp.json");

const EMPTY: GatewayConfig = { version: 1, agents: [] };

export async function loadConfig(): Promise<GatewayConfig> {
  try {
    const raw = await fs.readFile(cfgFile(), "utf8");
    const data = JSON.parse(raw);
    if (data?.version !== 1 || !Array.isArray(data.agents)) return { ...EMPTY };
    return data as GatewayConfig;
  } catch (err: any) {
    if (err?.code === "ENOENT") return { ...EMPTY };
    throw err;
  }
}

export async function saveConfig(cfg: GatewayConfig): Promise<void> {
  await fs.mkdir(cfgDir(), { recursive: true });
  await fs.writeFile(cfgFile(), JSON.stringify(cfg, null, 2), { mode: 0o600 });
}

export async function addAgent(agent: EnabledAgent): Promise<GatewayConfig> {
  const cfg = await loadConfig();
  const existing = cfg.agents.findIndex((a) => a.name === agent.name);
  if (existing >= 0) cfg.agents[existing] = agent;
  else cfg.agents.push(agent);
  await saveConfig(cfg);
  return cfg;
}

export async function removeAgent(name: string): Promise<boolean> {
  const cfg = await loadConfig();
  const before = cfg.agents.length;
  cfg.agents = cfg.agents.filter((a) => a.name !== name);
  if (cfg.agents.length === before) return false;
  await saveConfig(cfg);
  return true;
}
