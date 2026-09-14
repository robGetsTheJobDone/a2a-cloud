export type AgentDetailRouteStatus = "loading" | "missing" | "error" | "preparing";

export type AgentRouteRecoveryAgent = {
  name: string;
  description?: string | null;
  status?: string | null;
  public?: boolean | null;
  latest_deployment?: {
    status?: string | null;
  } | null;
};

const REQUESTED_AGENT_SEARCH_PARAM = "requested_agent";
const ROUTE_MATCH_STOP_TOKENS = new Set(["a2a", "agent", "agents"]);

export function agentDetailRouteReference(agentName: string): string {
  const readableName = agentName
    .trim()
    .replace(/[-_]+/g, " ")
    .replace(/\s+/g, " ");
  return `my agents/${readableName || "unknown agent"}`;
}

export function agentDetailRouteStatus({
  loading,
  error,
}: {
  loading: boolean;
  error: string | null;
}): AgentDetailRouteStatus {
  if (loading) return "loading";
  if (!error) return "preparing";
  return /^404(?::|\b)/.test(error.trim()) ? "missing" : "error";
}

export function requestedAgentNameFromSearch(search: string): string | null {
  const requested = new URLSearchParams(search).get(REQUESTED_AGENT_SEARCH_PARAM)
    ?.trim();
  return requested || null;
}

export function agentSearchWithRequestedAgent(
  search: string,
  agentName: string | null,
): string {
  const params = new URLSearchParams(search);
  const requested = agentName?.trim();
  if (requested) {
    params.set(REQUESTED_AGENT_SEARCH_PARAM, requested);
  } else {
    params.delete(REQUESTED_AGENT_SEARCH_PARAM);
  }
  const next = params.toString();
  return next ? `?${next}` : "";
}

export function agentSearchWithoutRequestedAgent(search: string): string {
  return agentSearchWithRequestedAgent(search, null);
}

export function agentRouteRecoveryMatches(
  agentName: string,
  agents: AgentRouteRecoveryAgent[],
  limit = 3,
): AgentRouteRecoveryAgent[] {
  const requested = normalizeRouteMatchValue(agentName);
  const requestedTokens = routeMatchTokens(agentName);
  if (!requested || requestedTokens.length === 0) return [];

  return agents
    .map((agent) => ({
      agent,
      score: agentRouteMatchScore(requested, requestedTokens, agent.name),
    }))
    .filter((candidate) => candidate.score > 0)
    .sort((left, right) => {
      if (right.score !== left.score) return right.score - left.score;
      return left.agent.name.localeCompare(right.agent.name);
    })
    .slice(0, Math.max(0, limit))
    .map((candidate) => candidate.agent);
}

function agentRouteMatchScore(
  requested: string,
  requestedTokens: string[],
  agentName: string,
): number {
  const candidate = normalizeRouteMatchValue(agentName);
  if (!candidate) return 0;
  if (candidate === requested) return 1000;
  if (candidate.startsWith(requested) || requested.startsWith(candidate)) {
    return 700;
  }
  if (candidate.includes(requested) || requested.includes(candidate)) {
    return 500;
  }

  const candidateTokens = new Set(routeMatchTokens(agentName));
  const overlap = requestedTokens.filter((token) => candidateTokens.has(token));
  if (overlap.length === 0) return 0;
  const coverage = overlap.length / requestedTokens.length;
  return overlap.length * 20 + Math.round(coverage * 100);
}

function routeMatchTokens(value: string): string[] {
  return normalizeRouteMatchValue(value)
    .split(" ")
    .filter((token) => token.length > 2 && !ROUTE_MATCH_STOP_TOKENS.has(token));
}

function normalizeRouteMatchValue(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}
