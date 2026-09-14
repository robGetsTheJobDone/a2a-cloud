import type { AgentListing } from "./api";

export type KernelBuilderAgent = Pick<AgentListing, "name" | "description" | "status" | "card">;

export type KernelBuilderInput = {
  simulationType: string;
  title: string;
  goal: string;
  agents: KernelBuilderAgent[];
};

export const BUILDER_KINDS = [
  "market",
  "tournament",
  "school",
  "adversarial_arena",
  "resource_competition",
  "capability_lifecycle",
  "partnership",
];

function safeNodeId(value: string, fallback: string) {
  const normalized = value.trim().replace(/[^a-zA-Z0-9_.:-]+/g, "-").replace(/^-+|-+$/g, "");
  return normalized || fallback;
}

function uniqueNodeId(baseId: string, usedIds: Set<string>) {
  let candidate = baseId;
  let suffix = 2;
  while (usedIds.has(candidate)) {
    candidate = `${baseId}-${suffix}`;
    suffix += 1;
  }
  usedIds.add(candidate);
  return candidate;
}

export function defaultSkill(agent: KernelBuilderAgent) {
  return agent.card?.skills?.[0]?.name || "pursue";
}

function scoreForAgent(index: number, count: number) {
  if (count <= 1) return 90;
  return Math.max(65, 95 - index * 7);
}

function roleFor(simulationType: string, index: number) {
  if (simulationType === "school") return index === 0 ? "mentor" : "learner";
  if (simulationType === "partnership") return index === 0 ? "lead_partner" : "partner";
  if (simulationType === "adversarial_arena") return index === 0 ? "subject" : "reviewer";
  if (simulationType === "capability_lifecycle") return index === 0 ? "incumbent" : "candidate";
  return "participant";
}

export function buildAgentKernelSimulationSpec({
  simulationType,
  title,
  goal,
  agents,
}: KernelBuilderInput): Record<string, unknown> {
  const chosen = agents.slice(0, 8);
  if (chosen.length === 0) {
    throw new Error("Choose at least one agent.");
  }
  const coordinatorId = "kernel-orchestrator";
  const usedNodeIds = new Set([coordinatorId]);
  const participants = chosen.map((agent, index) => {
    const nodeId = uniqueNodeId(safeNodeId(agent.name, `agent-${index + 1}`), usedNodeIds);
    const skill = safeNodeId(defaultSkill(agent), "pursue");
    return {
      agent,
      nodeId,
      skill,
      portId: `invoke:${skill}`,
      edgeId: `edge-kernel-${nodeId}`,
      outcomeId: `outcome-${nodeId}`,
      scoreId: `score-${nodeId}`,
      score: scoreForAgent(index, chosen.length),
      role: roleFor(simulationType, index),
    };
  });
  const edgeResources = participants.map((participant) => `${participant.nodeId}:${participant.portId}`);
  const steps: Array<Record<string, unknown>> = [
    {
      type: "start_process",
      process_id: "builder-simulation-1",
      owner: coordinatorId,
      capability_id: "cap-kernel-builder",
      ttl: 10,
      budget: Math.max(8, participants.length * 3),
    },
  ];

  for (const participant of participants) {
    const metrics: Record<string, number> = {
      score: participant.score / 100,
      cost: 1,
    };
    if (simulationType === "school") {
      metrics.capability_growth = participant.score / 100;
    }
    steps.push(
      {
        type: "propose_edge",
        edge_id: participant.edgeId,
        from: { node_id: coordinatorId, port_id: "route:task" },
        to: { node_id: participant.nodeId, port_id: participant.portId },
        edge_type: "call",
        capability_id: "cap-kernel-builder",
        process_id: "builder-simulation-1",
        provenance_ref: "dashboard:kernel-template-builder@v1",
      },
      {
        type: "activate_edge",
        edge_id: participant.edgeId,
        decision_id: `pd-${participant.nodeId}`,
      },
      { type: "use_edge", edge_id: participant.edgeId, cost: 1 },
      {
        type: "emit_signal",
        node_id: participant.nodeId,
        signal_type: `${simulationType}_evidence`,
        payload: {
          agent: participant.agent.name,
          skill: participant.skill,
          role: participant.role,
          score: participant.score / 100,
        },
      },
      {
        type: "record_outcome",
        outcome_id: participant.outcomeId,
        participant_id: participant.nodeId,
        process_id: "builder-simulation-1",
        edge_id: participant.edgeId,
        metrics,
        evidence_refs: [`signal:${simulationType}_evidence`],
      },
      {
        type: "score_participant",
        score_id: participant.scoreId,
        participant_id: participant.nodeId,
        outcome_id: participant.outcomeId,
        score: participant.score,
        explanation_refs: [`signal:${simulationType}_evidence`],
      },
    );
  }

  steps.push(
    {
      type: "select_winner",
      arena_id: "builder-arena-1",
      candidates: Object.fromEntries(
        participants.map((participant) => [
          participant.nodeId,
          {
            score_id: participant.scoreId,
            outcome_id: participant.outcomeId,
            edge_id: participant.edgeId,
            capability_id: "cap-kernel-builder",
          },
        ]),
      ),
    },
    {
      type: "check_policy",
      decision_id: "pd-builder-summary",
      action: "call",
      resource: edgeResources[0],
    },
    {
      type: "stop_process",
      process_id: "builder-simulation-1",
      reason: "simulation_completed",
    },
  );

  return {
    simulation_type: simulationType,
    title,
    goal,
    scenario_id: safeNodeId(title.toLowerCase(), "agent-template-builder"),
    actors: [
      { id: coordinatorId, label: "Kernel orchestrator", role: "coordinator" },
      ...participants.map((participant) => ({
        id: participant.nodeId,
        label: participant.agent.name,
        role: participant.role,
      })),
    ],
    ports: [
      {
        node_id: coordinatorId,
        id: "route:task",
        direction: "output",
        schema_ref: "kernel.builder.task.v1",
        port_type: "route",
      },
      ...participants.map((participant) => ({
        node_id: participant.nodeId,
        id: participant.portId,
        direction: "input",
        schema_ref: "agent.skill.v1",
        port_type: "invoke",
      })),
    ],
    capabilities: [
      {
        id: "cap-kernel-builder",
        owner: coordinatorId,
        actions: ["call"],
        resources: edgeResources,
        budget: Math.max(8, participants.length * 3),
        delegation_depth: 1,
      },
    ],
    policies: [
      {
        id: "owner-allow-builder-calls",
        level: "owner",
        effect: "allow",
        actions: ["call"],
        resources: edgeResources,
      },
    ],
    steps,
    invariants: [
      "replay_deterministic",
      "no_active_apply",
      "no_violations",
      { id: "process_absent", process_id: "builder-simulation-1" },
      ...participants.map((participant) => ({
        id: "outcome_recorded",
        outcome_id: participant.outcomeId,
        participant_id: participant.nodeId,
      })),
    ],
    metadata: {
      builder: "dashboard.kernel-template-builder@v1",
      source_agent_names: participants.map((participant) => participant.agent.name),
      source_agent_skills: Object.fromEntries(
        participants.map((participant) => [participant.agent.name, participant.skill]),
      ),
    },
  };
}
