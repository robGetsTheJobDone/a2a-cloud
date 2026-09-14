import { describe, expect, it } from "vitest";
import type {
  AgentDeployment,
  AgentMineSummary,
  AgentProofRun,
  SubagentRun,
} from "../api";
import { DEFAULT_AGENT_LIFECYCLE_FILTERS } from "./AgentLifecycleFilters";
import { filterMyAgents } from "./myAgentsFleet";

const runtimeUpgrade = {
  package: "a2a-pack",
  current_version: "0.1.0",
  latest_version: "0.1.0",
  update_available: false,
  can_redeploy: false,
  message: "current",
};

function agent(
  name: string,
  overrides: Partial<AgentMineSummary> = {},
): AgentMineSummary {
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
    ...overrides,
  };
}

function deployment(
  agentName: string,
  status: AgentDeployment["status"],
): AgentDeployment {
  return {
    id: 1,
    deploy_id: `deploy-${agentName}`,
    agent_name: agentName,
    trigger: "source_push",
    status,
    source_repo_url: null,
    head_sha: null,
    image: null,
    agent_url: null,
    error: null,
    verification: {},
    created_at: "2026-06-01T00:00:00.000Z",
    updated_at: "2026-06-01T00:00:00.000Z",
    started_at: null,
    completed_at: null,
    events: [],
  };
}

function run(agentName: string, status: string): SubagentRun {
  return {
    grant_id: `grant-${agentName}`,
    rerun_of_grant_id: null,
    thread_id: null,
    agent_name: agentName,
    skill_name: "work",
    args_json: "{}",
    scopes: {
      bucket: "bucket",
      mode: "read_write_overlay",
      allow_patterns: ["**"],
      deny_patterns: [],
      outputs_prefix: null,
      write_prefixes: [],
      ttl_seconds: 300,
    },
    status,
    summary: null,
    file_ops: [],
    created_at: "2026-06-01T00:00:00.000Z",
    updated_at: "2026-06-01T00:00:00.000Z",
    completed_at: null,
    events: [],
  };
}

function proof(
  agentName: string,
  status: string,
  badge: AgentProofRun["badge"],
  error: string | null = null,
): AgentProofRun {
  return {
    id: agentName.length,
    agent_name: agentName,
    skill_name: "prove",
    grant_id: null,
    status,
    badge,
    summary: null,
    error,
    args_preview: {},
    result: {},
    events: [],
    file_ops: [],
    card_hash: null,
    repo_url: null,
    head_sha: null,
    image: null,
    agent_url: null,
    elapsed_ms: null,
    created_at: "2026-06-01T00:00:00.000Z",
    started_at: null,
    completed_at: null,
  };
}

describe("my agents fleet filtering", () => {
  it("shows agents with failed deployments, runs, or proofs under the failed filter", () => {
    const agents = [
      agent("deploy-failed", {
        latest_deployment: deployment("deploy-failed", "failed"),
      }),
      agent("run-failed"),
      agent("proof-failed"),
      agent("healthy"),
    ];

    const filtered = filterMyAgents(
      agents,
      {
        ...DEFAULT_AGENT_LIFECYCLE_FILTERS,
        status: "failed",
        sort: "name-asc",
      },
      [
        proof("proof-failed", "passed", "verified", "receipt mismatch"),
        proof("healthy", "passed", "verified"),
      ],
      [run("run-failed", "denied")],
    );

    expect(filtered.map((item) => item.name)).toEqual([
      "deploy-failed",
      "proof-failed",
      "run-failed",
    ]);
  });

  it("does not treat proof coverage gaps as failed work", () => {
    const filtered = filterMyAgents(
      [agent("needs-proof"), agent("degraded-proof")],
      {
        ...DEFAULT_AGENT_LIFECYCLE_FILTERS,
        status: "failed",
      },
      [
        proof("needs-proof", "passed", "unverified"),
        proof("degraded-proof", "passed", "degraded"),
      ],
      [],
    );

    expect(filtered).toEqual([]);
  });
});
