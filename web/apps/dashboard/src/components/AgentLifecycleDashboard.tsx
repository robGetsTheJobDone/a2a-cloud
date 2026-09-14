import type { AgentMineSummary, AgentProofRun, SubagentRun } from "../api";

type AgentLifecycleHealthBucket =
  | "healthy"
  | "deploying"
  | "updateNeeded"
  | "needsProof"
  | "attention";

export type AgentLifecycleSummary = {
  totalAgents: number;
  deployLiveCount: number;
  updateNeededCount: number;
  failureCount: number;
  health: Record<AgentLifecycleHealthBucket, number>;
  proofCoverage: {
    verified: number;
    total: number;
    percent: number;
    degraded: number;
    unverified: number;
    missing: number;
  };
  failureBreakdown: {
    deployments: number;
    runs: number;
    proofs: number;
  };
};

export function summarizeAgentLifecycle(
  agents: readonly AgentMineSummary[],
  runs: readonly SubagentRun[],
  proofs: readonly AgentProofRun[],
): AgentLifecycleSummary {
  const agentNames = new Set(agents.map((agent) => agent.name));
  const latestProofs = latestProofByAgent(proofs, agentNames);
  const runFailuresByAgent = failureCountsByAgent(runs, agentNames);
  const proofFailuresByAgent = failureCountsByAgent(proofs, agentNames);
  const health = emptyHealthBuckets();

  let deployLiveCount = 0;
  let updateNeededCount = 0;
  let deploymentFailures = 0;
  let verifiedProofs = 0;
  let degradedProofs = 0;
  let unverifiedProofs = 0;
  let missingProofs = 0;

  for (const agent of agents) {
    const latestProof = latestProofs.get(agent.name) || null;
    const deploymentFailed = isFailureStatus(agent.latest_deployment?.status);
    const agentFailed = isFailureStatus(agent.status);

    if (isDeployLive(agent)) deployLiveCount += 1;
    if (agent.runtime_upgrade?.update_available) updateNeededCount += 1;
    if (deploymentFailed || agentFailed) deploymentFailures += 1;

    if (!latestProof) {
      missingProofs += 1;
    } else if (isVerifiedProof(latestProof)) {
      verifiedProofs += 1;
    } else if (latestProof.badge === "degraded") {
      degradedProofs += 1;
    } else {
      unverifiedProofs += 1;
    }

    const bucket = healthBucketForAgent({
      agent,
      latestProof,
      hasCurrentFailure: deploymentFailed || agentFailed,
    });
    health[bucket] += 1;
  }

  const runFailures = totalCount(runFailuresByAgent);
  const proofFailures = totalCount(proofFailuresByAgent);
  const totalAgents = agents.length;

  return {
    totalAgents,
    deployLiveCount,
    updateNeededCount,
    failureCount: deploymentFailures + runFailures + proofFailures,
    health,
    proofCoverage: {
      verified: verifiedProofs,
      total: totalAgents,
      percent: percent(verifiedProofs, totalAgents),
      degraded: degradedProofs,
      unverified: unverifiedProofs,
      missing: missingProofs,
    },
    failureBreakdown: {
      deployments: deploymentFailures,
      runs: runFailures,
      proofs: proofFailures,
    },
  };
}

function healthBucketForAgent({
  agent,
  latestProof,
  hasCurrentFailure,
}: {
  agent: AgentMineSummary;
  latestProof: AgentProofRun | null;
  hasCurrentFailure: boolean;
}): AgentLifecycleHealthBucket {
  if (hasCurrentFailure) return "attention";
  if (isDeploying(agent)) return "deploying";
  if (isDeployLive(agent)) return "healthy";
  if (agent.runtime_upgrade?.update_available) return "updateNeeded";
  if (!isVerifiedProof(latestProof)) return "needsProof";
  return "attention";
}

function latestProofByAgent(
  proofs: readonly AgentProofRun[],
  agentNames: ReadonlySet<string>,
) {
  const latestProofs = new Map<string, AgentProofRun>();

  for (const proof of proofs) {
    if (!agentNames.has(proof.agent_name)) continue;
    const current = latestProofs.get(proof.agent_name);
    if (!current || timestamp(proof.created_at) > timestamp(current.created_at)) {
      latestProofs.set(proof.agent_name, proof);
    }
  }

  return latestProofs;
}

function failureCountsByAgent(
  items: readonly { agent_name: string; status: string; error?: string | null }[],
  agentNames: ReadonlySet<string>,
) {
  const counts = new Map<string, number>();

  for (const item of items) {
    if (!agentNames.has(item.agent_name) || !isFailedItem(item)) continue;
    counts.set(item.agent_name, (counts.get(item.agent_name) || 0) + 1);
  }

  return counts;
}

function isDeploying(agent: AgentMineSummary) {
  const deploymentStatus = normalizeStatus(agent.latest_deployment?.status);
  const agentStatus = normalizeStatus(agent.status);
  return (
    isActiveDeploymentStatus(deploymentStatus) ||
    ["building", "deploying", "provisioning", "queued", "verifying"].includes(agentStatus)
  );
}

function isDeployLive(agent: AgentMineSummary) {
  const deploymentStatus = normalizeStatus(agent.latest_deployment?.status);
  const agentStatus = normalizeStatus(agent.status);
  return deploymentStatus === "live" || agentStatus === "running" || agentStatus === "ready";
}

function isVerifiedProof(proof: AgentProofRun | null) {
  return Boolean(proof && proof.badge === "verified" && proof.status === "passed");
}

function isFailedItem(item: { status: string; error?: string | null }) {
  return isFailureStatus(item.status) || Boolean(item.error);
}

function isFailureStatus(status?: string | null) {
  return ["denied", "error", "failed", "failure", "cancelled", "canceled"].includes(
    normalizeStatus(status),
  );
}

function isActiveDeploymentStatus(status?: string | null) {
  return ["queued", "building", "deploying", "verifying"].includes(
    normalizeStatus(status),
  );
}

function normalizeStatus(status?: string | null) {
  return (status || "").trim().toLowerCase();
}

function emptyHealthBuckets(): Record<AgentLifecycleHealthBucket, number> {
  return {
    healthy: 0,
    deploying: 0,
    updateNeeded: 0,
    needsProof: 0,
    attention: 0,
  };
}

function totalCount(counts: ReadonlyMap<string, number>) {
  let total = 0;
  for (const count of counts.values()) total += count;
  return total;
}

function percent(part: number, total: number) {
  if (total <= 0) return 0;
  return Math.round((part / total) * 100);
}

function timestamp(value: string) {
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : 0;
}
