import type { AgentMineSummary, AgentProofRun, SubagentRun } from "../api";
import {
  DEFAULT_AGENT_LIFECYCLE_FILTERS,
  type AgentLifecycleFilterValue,
} from "./AgentLifecycleFilters";
import {
  isActiveDeploymentStatus,
  isTransientAgentStatus,
} from "./agentStatus";

export { isActiveDeploymentStatus, isTransientAgentStatus } from "./agentStatus";

export function filterMyAgents(
  agents: AgentMineSummary[],
  filters: AgentLifecycleFilterValue,
  proofs: AgentProofRun[],
  runs: SubagentRun[] = [],
): AgentMineSummary[] {
  const query = filters.query.trim().toLowerCase();
  const latestProofs = latestProofsByAgent(proofs);
  const failedAgentNames = failedLifecycleAgentNames(runs, proofs);

  return agents
    .filter((agent) => {
      const latestProof = latestProofs.get(agent.name) || null;
      const proofBadge = latestProof?.badge || "unverified";
      const lifecycleStatus = agentLifecycleStatus(agent);
      const hasRecentFailure = failedAgentNames.has(agent.name);

      if (filters.status === "failed") {
        if (lifecycleStatus !== "failed" && !hasRecentFailure) return false;
      } else if (filters.status !== "all" && lifecycleStatus !== filters.status) {
        return false;
      }
      if (filters.visibility === "public" && !agent.public) return false;
      if (filters.visibility === "private" && agent.public) return false;
      if (filters.proof !== "all" && proofBadge !== filters.proof) return false;
      if (
        filters.runtimeUpdate === "update-available" &&
        !agent.runtime_upgrade?.update_available
      ) {
        return false;
      }
      if (
        filters.runtimeUpdate === "up-to-date" &&
        agent.runtime_upgrade?.update_available
      ) {
        return false;
      }
      if (query && !agentSearchText(agent, latestProof).includes(query)) {
        return false;
      }
      return true;
    })
    .slice()
    .sort((a, b) => sortAgents(a, b, filters.sort, latestProofs));
}

export function agentFiltersActive(filters: AgentLifecycleFilterValue) {
  return (
    filters.query.trim().length > 0 ||
    filters.status !== DEFAULT_AGENT_LIFECYCLE_FILTERS.status ||
    filters.visibility !== DEFAULT_AGENT_LIFECYCLE_FILTERS.visibility ||
    filters.proof !== DEFAULT_AGENT_LIFECYCLE_FILTERS.proof ||
    filters.runtimeUpdate !== DEFAULT_AGENT_LIFECYCLE_FILTERS.runtimeUpdate ||
    filters.sort !== DEFAULT_AGENT_LIFECYCLE_FILTERS.sort
  );
}

export function groupRunsByAgent(runs: SubagentRun[]): Map<string, SubagentRun[]> {
  const grouped = new Map<string, SubagentRun[]>();
  for (const run of runs) {
    const rows = grouped.get(run.agent_name);
    if (rows) rows.push(run);
    else grouped.set(run.agent_name, [run]);
  }
  return grouped;
}

export function groupProofsByAgent(
  proofs: AgentProofRun[],
): Map<string, AgentProofRun[]> {
  const grouped = new Map<string, AgentProofRun[]>();
  for (const proof of proofs) {
    const rows = grouped.get(proof.agent_name);
    if (rows) rows.push(proof);
    else grouped.set(proof.agent_name, [proof]);
  }
  return grouped;
}

export function latestProofsByAgent(
  proofs: AgentProofRun[],
): Map<string, AgentProofRun> {
  const latest = new Map<string, AgentProofRun>();
  for (const proof of proofs) {
    const current = latest.get(proof.agent_name);
    if (!current || dateMs(proof.created_at) > dateMs(current.created_at)) {
      latest.set(proof.agent_name, proof);
    }
  }
  return latest;
}

export function canBulkUpgradeRuntime(agent: AgentMineSummary): boolean {
  return (
    Boolean(agent.runtime_upgrade?.can_redeploy) &&
    !isActiveDeploymentStatus(agent.latest_deployment?.status) &&
    !isTransientAgentStatus(agent.status)
  );
}

export function agentStatusFromDeployment(
  current: string,
  deploymentStatus: string,
): string {
  if (deploymentStatus === "live") return "running";
  if (deploymentStatus === "failed") return "failed";
  if (isActiveDeploymentStatus(deploymentStatus)) return deploymentStatus;
  return current;
}

function agentLifecycleStatus(
  agent: AgentMineSummary,
): AgentLifecycleFilterValue["status"] {
  const status = agent.status.toLowerCase();
  const deployStatus = agent.latest_deployment?.status?.toLowerCase() || null;
  if (
    status === "failed" ||
    status === "error" ||
    status === "crashed" ||
    deployStatus === "failed"
  ) {
    return "failed";
  }
  if (
    isActiveDeploymentStatus(deployStatus) ||
    isTransientAgentStatus(status)
  ) {
    return "deploying";
  }
  if (status === "running" || status === "ready") return "running";
  return "inactive";
}

function agentSearchText(
  agent: AgentMineSummary,
  latestProof: AgentProofRun | null,
): string {
  return [
    agent.name,
    agent.description,
    agent.status,
    agent.version,
    agent.latest_deployment?.status,
    latestProof?.badge,
    latestProof?.skill_name,
    latestProof?.summary,
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

function sortAgents(
  a: AgentMineSummary,
  b: AgentMineSummary,
  sort: AgentLifecycleFilterValue["sort"],
  latestProofs: Map<string, AgentProofRun>,
): number {
  if (sort === "created-asc") return dateMs(a.created_at) - dateMs(b.created_at);
  if (sort === "created-desc") return dateMs(b.created_at) - dateMs(a.created_at);
  if (sort === "name-asc") return a.name.localeCompare(b.name);
  if (sort === "name-desc") return b.name.localeCompare(a.name);
  if (sort === "status") {
    return (
      agentLifecycleStatus(a).localeCompare(agentLifecycleStatus(b)) ||
      a.name.localeCompare(b.name)
    );
  }
  if (sort === "proof") {
    return (
      proofRank(latestProofs.get(a.name)?.badge) -
        proofRank(latestProofs.get(b.name)?.badge) ||
      a.name.localeCompare(b.name)
    );
  }
  if (sort === "runtime-update") {
    return (
      Number(Boolean(b.runtime_upgrade?.update_available)) -
        Number(Boolean(a.runtime_upgrade?.update_available)) ||
      a.name.localeCompare(b.name)
    );
  }
  return 0;
}

function proofRank(badge?: string | null): number {
  if (badge === "verified") return 0;
  if (badge === "degraded") return 1;
  return 2;
}

function failedLifecycleAgentNames(
  runs: SubagentRun[],
  proofs: AgentProofRun[],
): Set<string> {
  const failed = new Set<string>();
  for (const run of runs) {
    if (isFailedWorkItem(run)) failed.add(run.agent_name);
  }
  for (const proof of proofs) {
    if (isFailedWorkItem(proof)) failed.add(proof.agent_name);
  }
  return failed;
}

function isFailedWorkItem(item: {
  status: string;
  error?: string | null;
}): boolean {
  return (
    ["denied", "error", "failed", "failure", "cancelled", "canceled"].includes(
      item.status.toLowerCase(),
    ) || Boolean(item.error)
  );
}

function dateMs(value: string): number {
  const ms = new Date(value).getTime();
  return Number.isNaN(ms) ? 0 : ms;
}
