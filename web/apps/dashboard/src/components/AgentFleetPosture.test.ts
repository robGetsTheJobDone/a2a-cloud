import { describe, expect, it } from "vitest";
import type { AgentMineSummary } from "../api";
import type { AgentLifecycleSummary } from "./AgentLifecycleDashboard";
import { agentFleetNextAction } from "./AgentFleetPosture";

const runtimeUpgrade = {
  package: "a2a-pack",
  current_version: "0.1.0",
  latest_version: "0.1.0",
  update_available: false,
  can_redeploy: false,
  message: "current",
};

function agent(name: string): AgentMineSummary {
  return {
    id: name.length,
    name,
    description: `${name} agent`,
    version: "1.0.0",
    public: true,
    status: "running",
    url: `https://${name}.example.test`,
    created_at: "2026-06-01T00:00:00.000Z",
    skill_count: 1,
    runtime_upgrade: runtimeUpgrade,
    latest_deployment: null,
  };
}

type SummaryPatch = Partial<
  Omit<AgentLifecycleSummary, "health" | "proofCoverage" | "failureBreakdown">
> & {
  health?: Partial<AgentLifecycleSummary["health"]>;
  proofCoverage?: Partial<AgentLifecycleSummary["proofCoverage"]>;
  failureBreakdown?: Partial<AgentLifecycleSummary["failureBreakdown"]>;
};

function summary(patch: SummaryPatch = {}): AgentLifecycleSummary {
  const base: AgentLifecycleSummary = {
    totalAgents: 1,
    deployLiveCount: 1,
    updateNeededCount: 0,
    failureCount: 0,
    health: {
      healthy: 1,
      deploying: 0,
      updateNeeded: 0,
      needsProof: 0,
      attention: 0,
    },
    proofCoverage: {
      verified: 1,
      total: 1,
      percent: 100,
      degraded: 0,
      unverified: 0,
      missing: 0,
    },
    failureBreakdown: {
      deployments: 0,
      runs: 0,
      proofs: 0,
    },
  };

  return {
    ...base,
    ...patch,
    health: { ...base.health, ...patch.health },
    proofCoverage: { ...base.proofCoverage, ...patch.proofCoverage },
    failureBreakdown: { ...base.failureBreakdown, ...patch.failureBreakdown },
  };
}

function noop() {
  return undefined;
}

type NextActionInput = Parameters<typeof agentFleetNextAction>[0];

function nextAction(patch: Partial<NextActionInput> = {}) {
  const alpha = agent("alpha");
  return agentFleetNextAction({
    search: "",
    agents: [alpha],
    filtered: [alpha],
    summary: summary(),
    hasActiveFilters: false,
    selectedAgentName: null,
    selectedAgent: null,
    bulkUpgradeCount: 0,
    bulkUpgradeBusy: false,
    proofGapCount: 0,
    onClearFilters: noop,
    onShowFailed: noop,
    onShowDeploying: noop,
    onShowProofGaps: noop,
    onShowRuntimeUpdates: noop,
    onUpgradeAll: noop,
    ...patch,
  });
}

describe("agentFleetNextAction", () => {
  it("prioritizes active deployments before maintenance actions", () => {
    const action = nextAction({
      summary: summary({
        health: { healthy: 0, deploying: 1 },
        deployLiveCount: 0,
        updateNeededCount: 1,
      }),
      bulkUpgradeCount: 1,
    });

    expect(action.kind).toBe("button");
    expect(action.action).toBe("Show deploying");
    expect(action.label).toBe("Watch active deployments");
  });

  it("routes deployment failures to the failure filter first", () => {
    const action = nextAction({
      summary: summary({
        failureCount: 3,
        failureBreakdown: { deployments: 1, runs: 2, proofs: 0 },
        health: { attention: 1 },
      }),
    });

    expect(action.kind).toBe("button");
    expect(action.action).toBe("Show deploy failures");
    expect(action.label).toBe("Review failed deployments");
    expect(action.variant).toBe("danger");
  });

  it("routes run-only failures to run review copy", () => {
    const action = nextAction({
      summary: summary({
        failureCount: 2,
        failureBreakdown: { deployments: 0, runs: 2, proofs: 0 },
        health: { attention: 1 },
      }),
    });

    expect(action.kind).toBe("button");
    expect(action.action).toBe("Show run failures");
    expect(action.label).toBe("Review failed runs");
  });

  it("routes proof-only failures to proof receipt review copy", () => {
    const action = nextAction({
      summary: summary({
        failureCount: 1,
        failureBreakdown: { deployments: 0, runs: 0, proofs: 1 },
        health: { attention: 1 },
      }),
    });

    expect(action.kind).toBe("button");
    expect(action.action).toBe("Show proof failures");
    expect(action.label).toBe("Review failed proofs");
  });

  it("preserves route search when linking to the top fleet result", () => {
    const action = nextAction({ search: "?workspace=ops" });

    expect(action.kind).toBe("link");
    if (action.kind !== "link") throw new Error("expected link action");
    expect(action.href).toBe("/my-agents/alpha?workspace=ops");
  });

  it("keeps a readable route reference visible while a deep-linked agent resolves", () => {
    const action = nextAction({
      selectedAgentName: "app-flow-smoke-20260625t173231449-529287",
      selectedAgent: null,
    });

    expect(action.kind).toBe("link");
    expect(action.label).toBe(
      "Resolve my agents/app flow smoke 20260625t173231449 529287",
    );
  });
});
